package com.linkedin.metadata.search.elasticsearch.query.request;

import com.google.common.annotations.VisibleForTesting;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.ImmutableMap;
import com.linkedin.common.urn.Urn;
import com.linkedin.data.template.DoubleMap;
import com.linkedin.data.template.LongMap;
import com.linkedin.metadata.config.search.SearchConfiguration;
import com.linkedin.metadata.config.search.custom.CustomSearchConfiguration;
import com.linkedin.metadata.models.EntitySpec;
import com.linkedin.metadata.models.SearchableFieldSpec;
import com.linkedin.metadata.models.annotation.SearchableAnnotation;
import com.linkedin.metadata.query.SearchFlags;
import com.linkedin.metadata.query.filter.ConjunctiveCriterion;
import com.linkedin.metadata.query.filter.ConjunctiveCriterionArray;
import com.linkedin.metadata.query.filter.Criterion;
import com.linkedin.metadata.query.filter.CriterionArray;
import com.linkedin.metadata.query.filter.Filter;
import com.linkedin.metadata.query.filter.SortCriterion;
import com.linkedin.metadata.search.AggregationMetadata;
import com.linkedin.metadata.search.AggregationMetadataArray;
import com.linkedin.metadata.search.FilterValueArray;
import com.linkedin.metadata.search.MatchedField;
import com.linkedin.metadata.search.MatchedFieldArray;
import com.linkedin.metadata.search.ScrollResult;
import com.linkedin.metadata.search.SearchEntity;
import com.linkedin.metadata.search.SearchEntityArray;
import com.linkedin.metadata.search.SearchResult;
import com.linkedin.metadata.search.SearchResultMetadata;
import com.linkedin.metadata.search.features.Features;
import com.linkedin.metadata.search.utils.ESUtils;
import com.linkedin.metadata.utils.SearchUtil;
import io.opentelemetry.extension.annotations.WithSpan;
import java.net.URISyntaxException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.BinaryOperator;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import javax.annotation.Nonnull;
import javax.annotation.Nullable;
import lombok.extern.slf4j.Slf4j;
import org.apache.commons.lang.StringUtils;
import org.elasticsearch.action.search.SearchRequest;
import org.elasticsearch.action.search.SearchResponse;
import org.elasticsearch.common.text.Text;
import org.elasticsearch.common.unit.TimeValue;
import org.elasticsearch.index.query.BoolQueryBuilder;
import org.elasticsearch.index.query.QueryBuilder;
import org.elasticsearch.index.query.QueryBuilders;
import org.elasticsearch.search.SearchHit;
import org.elasticsearch.search.aggregations.Aggregation;
import org.elasticsearch.search.aggregations.AggregationBuilder;
import org.elasticsearch.search.aggregations.AggregationBuilders;
import org.elasticsearch.search.aggregations.bucket.terms.ParsedTerms;
import org.elasticsearch.search.aggregations.bucket.terms.Terms;
import org.elasticsearch.search.builder.SearchSourceBuilder;
import org.elasticsearch.search.fetch.subphase.highlight.HighlightBuilder;
import org.elasticsearch.search.fetch.subphase.highlight.HighlightField;

import static com.linkedin.metadata.search.utils.ESUtils.KEYWORD_SUFFIX;
import static com.linkedin.metadata.search.utils.ESUtils.toFacetField;
import static com.linkedin.metadata.search.utils.SearchUtils.applyDefaultSearchFlags;
import static com.linkedin.metadata.utils.SearchUtil.createFilterValue;


@Slf4j
public class SearchRequestHandler {
  private static final SearchFlags DEFAULT_SERVICE_SEARCH_FLAGS = new SearchFlags()
          .setFulltext(false)
          .setMaxAggValues(20)
          .setSkipCache(false)
          .setSkipAggregates(false)
          .setSkipHighlighting(false);

  private static final Map<List<EntitySpec>, SearchRequestHandler> REQUEST_HANDLER_BY_ENTITY_NAME = new ConcurrentHashMap<>();
  private static final String REMOVED = "removed";

  private static final String URN_FILTER = "urn";
  private final List<EntitySpec> _entitySpecs;
  private final Set<String> _facetFields;
  private final Set<String> _defaultQueryFieldNames;
  private final HighlightBuilder _highlights;
  private final Map<String, String> _filtersToDisplayName;
  private final SearchConfiguration _configs;
  private final SearchQueryBuilder _searchQueryBuilder;


  private SearchRequestHandler(@Nonnull EntitySpec entitySpec, @Nonnull SearchConfiguration configs,
                               @Nullable CustomSearchConfiguration customSearchConfiguration) {
    this(ImmutableList.of(entitySpec), configs, customSearchConfiguration);
  }

  private SearchRequestHandler(@Nonnull List<EntitySpec> entitySpecs, @Nonnull SearchConfiguration configs,
                               @Nullable CustomSearchConfiguration customSearchConfiguration) {
    _entitySpecs = entitySpecs;
    List<SearchableAnnotation> annotations = getSearchableAnnotations();
    _facetFields = getFacetFields(annotations);
    _defaultQueryFieldNames = getDefaultQueryFieldNames(annotations);
    _filtersToDisplayName = annotations.stream()
        .filter(SearchableAnnotation::isAddToFilters)
        .collect(Collectors.toMap(SearchableAnnotation::getFieldName, SearchableAnnotation::getFilterName, mapMerger()));
    _highlights = getHighlights();
    _searchQueryBuilder = new SearchQueryBuilder(configs, customSearchConfiguration);
    _configs = configs;
  }

  public static SearchRequestHandler getBuilder(@Nonnull EntitySpec entitySpec, @Nonnull SearchConfiguration configs,
                                                @Nullable CustomSearchConfiguration customSearchConfiguration) {
    return REQUEST_HANDLER_BY_ENTITY_NAME.computeIfAbsent(
            ImmutableList.of(entitySpec), k -> new SearchRequestHandler(entitySpec, configs, customSearchConfiguration));
  }

  public static SearchRequestHandler getBuilder(@Nonnull List<EntitySpec> entitySpecs, @Nonnull SearchConfiguration configs,
                                                @Nullable CustomSearchConfiguration customSearchConfiguration) {
    return REQUEST_HANDLER_BY_ENTITY_NAME.computeIfAbsent(
            ImmutableList.copyOf(entitySpecs), k -> new SearchRequestHandler(entitySpecs, configs, customSearchConfiguration));
  }

  private List<SearchableAnnotation> getSearchableAnnotations() {
    return _entitySpecs.stream()
        .map(EntitySpec::getSearchableFieldSpecs)
        .flatMap(List::stream)
        .map(SearchableFieldSpec::getSearchableAnnotation)
        .collect(Collectors.toList());
  }

  private Set<String> getFacetFields(List<SearchableAnnotation> annotations) {
    return annotations.stream()
        .filter(SearchableAnnotation::isAddToFilters)
        .map(SearchableAnnotation::getFieldName)
        .collect(Collectors.toSet());
  }

  @VisibleForTesting
  private Set<String> getDefaultQueryFieldNames(List<SearchableAnnotation> annotations) {
    return Stream.concat(annotations.stream()
        .filter(SearchableAnnotation::isQueryByDefault)
        .map(SearchableAnnotation::getFieldName),
            Stream.of("urn"))
            .collect(Collectors.toSet());
  }

  // If values are not equal, throw error
  private BinaryOperator<String> mapMerger() {
    return (s1, s2) -> {
          if (!StringUtils.equals(s1, s2)) {
            throw new IllegalStateException(String.format("Unable to merge values %s and %s", s1, s2));
          }
          return s1;
      };
  }

  public static BoolQueryBuilder getFilterQuery(@Nullable Filter filter) {
    BoolQueryBuilder filterQuery = ESUtils.buildFilterQuery(filter, false);

    boolean removedInOrFilter = false;
    if (filter != null) {
      removedInOrFilter = filter.getOr().stream().anyMatch(
              or -> or.getAnd().stream().anyMatch(criterion -> criterion.getField().equals(REMOVED) || criterion.getField().equals(REMOVED + KEYWORD_SUFFIX))
      );
    }
    // Filter out entities that are marked "removed" if and only if filter does not contain a criterion referencing it.
    if (!removedInOrFilter) {
      filterQuery.mustNot(QueryBuilders.matchQuery(REMOVED, true));
    }

    return filterQuery;
  }

  /**
   * Constructs the search query based on the query request.
   *
   * <p>TODO: This part will be replaced by searchTemplateAPI when the elastic is upgraded to 6.4 or later
   *
   * @param input the search input text
   * @param filter the search filter
   * @param from index to start the search from
   * @param size the number of search hits to return
   * @param searchFlags Various flags controlling search query options
   * @return a valid search request
   */
  @Nonnull
  @WithSpan
  public SearchRequest getSearchRequest(@Nonnull String input, @Nullable Filter filter,
                                        @Nullable SortCriterion sortCriterion, int from, int size,
                                        @Nullable SearchFlags searchFlags) {
    SearchFlags finalSearchFlags = applyDefaultSearchFlags(searchFlags, input, DEFAULT_SERVICE_SEARCH_FLAGS);

    SearchRequest searchRequest = new SearchRequest();
    SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();

    searchSourceBuilder.from(from);
    searchSourceBuilder.size(size);
    searchSourceBuilder.fetchSource("urn", null);

    BoolQueryBuilder filterQuery = getFilterQuery(filter);
    searchSourceBuilder.query(QueryBuilders.boolQuery()
            .must(getQuery(input, finalSearchFlags.isFulltext()))
            .must(filterQuery));
    if (!finalSearchFlags.isSkipAggregates()) {
      getAggregations().forEach(searchSourceBuilder::aggregation);
    }
    if (!finalSearchFlags.isSkipHighlighting()) {
      searchSourceBuilder.highlighter(_highlights);
    }
    ESUtils.buildSortOrder(searchSourceBuilder, sortCriterion);
    searchRequest.source(searchSourceBuilder);
    log.debug("Search request is: " + searchRequest.toString());

    return searchRequest;
  }

  /**
   * Constructs the search query based on the query request.
   *
   * <p>TODO: This part will be replaced by searchTemplateAPI when the elastic is upgraded to 6.4 or later
   *
   * @param input the search input text
   * @param filter the search filter
   * @param sort sort values of the last result of the previous page
   * @param size the number of search hits to return
   * @return a valid search request
   */
  @Nonnull
  @WithSpan
  public SearchRequest getSearchRequest(@Nonnull String input, @Nullable Filter filter,
      @Nullable SortCriterion sortCriterion, @Nullable Object[] sort, @Nullable String pitId, @Nonnull String keepAlive,
      int size, SearchFlags searchFlags) {
    SearchRequest searchRequest = new PITAwareSearchRequest();
    SearchFlags finalSearchFlags = applyDefaultSearchFlags(searchFlags, input, DEFAULT_SERVICE_SEARCH_FLAGS);
    SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();

    ESUtils.setSearchAfter(searchSourceBuilder, sort, pitId, keepAlive);

    searchSourceBuilder.size(size);
    searchSourceBuilder.fetchSource("urn", null);

    BoolQueryBuilder filterQuery = getFilterQuery(filter);
    searchSourceBuilder.query(QueryBuilders.boolQuery().must(getQuery(input, finalSearchFlags.isFulltext())).must(filterQuery));
    getAggregations().forEach(searchSourceBuilder::aggregation);
    searchSourceBuilder.highlighter(getHighlights());
    ESUtils.buildSortOrder(searchSourceBuilder, sortCriterion);
    searchRequest.source(searchSourceBuilder);
    log.debug("Search request is: " + searchRequest);
    searchRequest.indicesOptions(null);

    return searchRequest;
  }

  /**
   * Returns a {@link SearchRequest} given filters to be applied to search query and sort criterion to be applied to
   * search results.
   *
   * @param filters {@link Filter} list of conditions with fields and values
   * @param sortCriterion {@link SortCriterion} to be applied to the search results
   * @param from index to start the search from
   * @param size the number of search hits to return
   * @return {@link SearchRequest} that contains the filtered query
   */
  @Nonnull
  public SearchRequest getFilterRequest(@Nullable Filter filters, @Nullable SortCriterion sortCriterion, int from,
      int size) {
    SearchRequest searchRequest = new SearchRequest();

    BoolQueryBuilder filterQuery = getFilterQuery(filters);
    final SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();
    searchSourceBuilder.query(filterQuery);
    searchSourceBuilder.from(from).size(size);
    ESUtils.buildSortOrder(searchSourceBuilder, sortCriterion);
    searchRequest.source(searchSourceBuilder);

    return searchRequest;
  }

  /**
   * Returns a {@link SearchRequest} given filters to be applied to search query and sort criterion to be applied to
   * search results.
   *
   * TODO: Used in batch ingestion from ingestion scheduler
   *
   * @param filters {@link Filter} list of conditions with fields and values
   * @param sortCriterion {@link SortCriterion} to be applied to the search results
   * @param sort sort values from last result of previous request
   * @param pitId the Point In Time Id of the previous request
   * @param keepAlive string representation of time to keep point in time alive
   * @param size the number of search hits to return
   * @return {@link SearchRequest} that contains the filtered query
   */
  @Nonnull
  public SearchRequest getFilterRequest(@Nullable Filter filters, @Nullable SortCriterion sortCriterion, @Nullable Object[] sort,
      @Nullable String pitId, @Nonnull String keepAlive, int size) {
    SearchRequest searchRequest = new SearchRequest();

    BoolQueryBuilder filterQuery = getFilterQuery(filters);
    final SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();
    searchSourceBuilder.query(filterQuery);
    searchSourceBuilder.size(size);

    ESUtils.setSearchAfter(searchSourceBuilder, sort, pitId, keepAlive);
    ESUtils.buildSortOrder(searchSourceBuilder, sortCriterion);
    searchRequest.source(searchSourceBuilder);

    return searchRequest;
  }

  /**
   * Get search request to aggregate and get document counts per field value
   *
   * @param field Field to aggregate by
   * @param filter {@link Filter} list of conditions with fields and values
   * @param limit number of aggregations to return
   * @return {@link SearchRequest} that contains the aggregation query
   */
  @Nonnull
  public static SearchRequest getAggregationRequest(@Nonnull String field, @Nullable Filter filter, int limit) {
    SearchRequest searchRequest = new SearchRequest();
    BoolQueryBuilder filterQuery = getFilterQuery(filter);

    final SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();
    searchSourceBuilder.query(filterQuery);
    searchSourceBuilder.size(0);
    searchSourceBuilder.aggregation(AggregationBuilders.terms(field).field(ESUtils.toKeywordField(field, false)).size(limit));
    searchRequest.source(searchSourceBuilder);

    return searchRequest;
  }

  private QueryBuilder getQuery(@Nonnull String query, boolean fulltext) {
    return _searchQueryBuilder.buildQuery(_entitySpecs, query, fulltext);
  }

  private List<AggregationBuilder> getAggregations() {
    List<AggregationBuilder> aggregationBuilders = new ArrayList<>();
    for (String facet : _facetFields) {
      // All facet fields must have subField keyword
      AggregationBuilder aggBuilder =
          AggregationBuilders.terms(facet).field(ESUtils.toKeywordField(facet, false)).size(_configs.getMaxTermBucketSize());
      aggregationBuilders.add(aggBuilder);
    }
    return aggregationBuilders;
  }

  @VisibleForTesting
  public HighlightBuilder getHighlights() {
    HighlightBuilder highlightBuilder = new HighlightBuilder();

    // Don't set tags to get the original field value
    highlightBuilder.preTags("");
    highlightBuilder.postTags("");

    // Check for each field name and any subfields
    _defaultQueryFieldNames.stream()
            .flatMap(fieldName -> Stream.of(fieldName, fieldName + ".*")).distinct()
            .forEach(highlightBuilder::field);

    return highlightBuilder;
  }

  @WithSpan
  public SearchResult extractResult(@Nonnull SearchResponse searchResponse, Filter filter, int from, int size) {
    int totalCount = (int) searchResponse.getHits().getTotalHits().value;
    List<SearchEntity> resultList = getResults(searchResponse);
    SearchResultMetadata searchResultMetadata = extractSearchResultMetadata(searchResponse, filter);

    return new SearchResult().setEntities(new SearchEntityArray(resultList))
        .setMetadata(searchResultMetadata)
        .setFrom(from)
        .setPageSize(size)
        .setNumEntities(totalCount);
  }

  @WithSpan
  public ScrollResult extractScrollResult(@Nonnull SearchResponse searchResponse, Filter filter, @Nullable String scrollId,
      @Nonnull String keepAlive, int size, boolean supportsPointInTime) {
    int totalCount = (int) searchResponse.getHits().getTotalHits().value;
    List<SearchEntity> resultList = getResults(searchResponse);
    SearchResultMetadata searchResultMetadata = extractSearchResultMetadata(searchResponse, filter);
    SearchHit[] searchHits = searchResponse.getHits().getHits();
    // Only return next scroll ID if there are more results, indicated by full size results
    String nextScrollId = null;
    if (searchHits.length == size) {
      Object[] sort = searchHits[searchHits.length - 1].getSortValues();
      long expirationTimeMs = 0L;
      if (supportsPointInTime) {
        expirationTimeMs = TimeValue.parseTimeValue(keepAlive, "expirationTime").getMillis() + System.currentTimeMillis();
      }
      nextScrollId = new SearchAfterWrapper(sort, searchResponse.pointInTimeId(), expirationTimeMs).toScrollId();
    }

    ScrollResult scrollResult = new ScrollResult().setEntities(new SearchEntityArray(resultList))
        .setMetadata(searchResultMetadata)
        .setPageSize(size)
        .setNumEntities(totalCount);

    if (nextScrollId != null) {
      scrollResult.setScrollId(nextScrollId);
    }
    return scrollResult;
  }

  @Nonnull
  private List<MatchedField> extractMatchedFields(@Nonnull SearchHit hit) {
    Map<String, HighlightField> highlightedFields = hit.getHighlightFields();
    // Keep track of unique field values that matched for a given field name
    Map<String, Set<String>> highlightedFieldNamesAndValues = new HashMap<>();
    for (Map.Entry<String, HighlightField> entry : highlightedFields.entrySet()) {
      // Get the field name from source e.g. name.delimited -> name
      Optional<String> fieldName = getFieldName(entry.getKey());
      if (!fieldName.isPresent()) {
        continue;
      }
      if (!highlightedFieldNamesAndValues.containsKey(fieldName.get())) {
        highlightedFieldNamesAndValues.put(fieldName.get(), new HashSet<>());
      }
      for (Text fieldValue : entry.getValue().getFragments()) {
        highlightedFieldNamesAndValues.get(fieldName.get()).add(fieldValue.string());
      }
    }
    // fallback matched query, non-analyzed field
    for (String queryName : hit.getMatchedQueries()) {
      if (!highlightedFieldNamesAndValues.containsKey(queryName)) {
        if (hit.getFields().containsKey(queryName)) {
          for (Object fieldValue : hit.getFields().get(queryName).getValues()) {
            highlightedFieldNamesAndValues.computeIfAbsent(queryName, k -> new HashSet<>()).add(fieldValue.toString());
          }
        } else {
          highlightedFieldNamesAndValues.put(queryName, Set.of(""));
        }
      }
    }
    return highlightedFieldNamesAndValues.entrySet()
        .stream()
        .flatMap(
            entry -> entry.getValue().stream().map(value -> new MatchedField().setName(entry.getKey()).setValue(value)))
        .collect(Collectors.toList());
  }

  @Nonnull
  private Optional<String> getFieldName(String matchedField) {
    return _defaultQueryFieldNames.stream().filter(matchedField::startsWith).findFirst();
  }

  private Map<String, Double> extractFeatures(@Nonnull SearchHit searchHit) {
    return ImmutableMap.of(Features.Name.SEARCH_BACKEND_SCORE.toString(), (double) searchHit.getScore());
  }

  private SearchEntity getResult(@Nonnull SearchHit hit) {
    return new SearchEntity().setEntity(getUrnFromSearchHit(hit))
        .setMatchedFields(new MatchedFieldArray(extractMatchedFields(hit)))
        .setScore(hit.getScore())
        .setFeatures(new DoubleMap(extractFeatures(hit)));
  }

  /**
   * Gets list of entities returned in the search response
   *
   * @param searchResponse the raw search response from search engine
   * @return List of search entities
   */
  @Nonnull
  private List<SearchEntity> getResults(@Nonnull SearchResponse searchResponse) {
    return Arrays.stream(searchResponse.getHits().getHits()).map(this::getResult).collect(Collectors.toList());
  }

  @Nonnull
  private Urn getUrnFromSearchHit(@Nonnull SearchHit hit) {
    try {
      return Urn.createFromString(hit.getSourceAsMap().get("urn").toString());
    } catch (URISyntaxException e) {
      throw new RuntimeException("Invalid urn in search document " + e);
    }
  }

  /**
   * Extracts SearchResultMetadata section.
   *
   * @param searchResponse the raw {@link SearchResponse} as obtained from the search engine
   * @param filter the provided Filter to use with Elasticsearch
   *
   * @return {@link SearchResultMetadata} with aggregation and list of urns obtained from {@link SearchResponse}
   */
  @Nonnull
  private SearchResultMetadata extractSearchResultMetadata(@Nonnull SearchResponse searchResponse, @Nullable Filter filter) {
    final SearchResultMetadata searchResultMetadata =
        new SearchResultMetadata().setAggregations(new AggregationMetadataArray());

    final List<AggregationMetadata> aggregationMetadataList = extractAggregationMetadata(searchResponse, filter);
    searchResultMetadata.setAggregations(new AggregationMetadataArray(aggregationMetadataList));

    return searchResultMetadata;
  }

  private List<AggregationMetadata> extractAggregationMetadata(@Nonnull SearchResponse searchResponse, @Nullable Filter filter) {
    final List<AggregationMetadata> aggregationMetadataList = new ArrayList<>();

    if (searchResponse.getAggregations() == null) {
      return addFiltersToAggregationMetadata(aggregationMetadataList, filter);
    }

    for (Map.Entry<String, Aggregation> entry : searchResponse.getAggregations().getAsMap().entrySet()) {
      final Map<String, Long> oneTermAggResult = extractTermAggregations((ParsedTerms) entry.getValue());
      if (oneTermAggResult.isEmpty()) {
        continue;
      }
      final AggregationMetadata aggregationMetadata = new AggregationMetadata().setName(entry.getKey())
          .setDisplayName(_filtersToDisplayName.get(entry.getKey()))
          .setAggregations(new LongMap(oneTermAggResult))
          .setFilterValues(new FilterValueArray(SearchUtil.convertToFilters(oneTermAggResult, Collections.emptySet())));
      aggregationMetadataList.add(aggregationMetadata);
    }

    return addFiltersToAggregationMetadata(aggregationMetadataList, filter);
  }

  @WithSpan
  public static Map<String, Long> extractTermAggregations(@Nonnull SearchResponse searchResponse,
      @Nonnull String aggregationName) {
    if (searchResponse.getAggregations() == null) {
      return Collections.emptyMap();
    }

    Aggregation aggregation = searchResponse.getAggregations().get(aggregationName);
    if (aggregation == null) {
      return Collections.emptyMap();
    }
    return extractTermAggregations((ParsedTerms) aggregation);
  }

  /**
   * Extracts term aggregations give a parsed term.
   *
   * @param terms an abstract parse term, input can be either ParsedStringTerms ParsedLongTerms
   * @return a map with aggregation key and corresponding doc counts
   */
  @Nonnull
  private static Map<String, Long> extractTermAggregations(@Nonnull ParsedTerms terms) {

    final Map<String, Long> aggResult = new HashMap<>();
    List<? extends Terms.Bucket> bucketList = terms.getBuckets();

    for (Terms.Bucket bucket : bucketList) {
      String key = bucket.getKeyAsString();
      // Gets filtered sub aggregation doc count if exist
      long docCount = bucket.getDocCount();
      if (docCount > 0) {
        aggResult.put(key, docCount);
      }
    }

    return aggResult;
  }

  /**
   * Injects the missing conjunctive filters into the aggregations list.
   */
  public List<AggregationMetadata> addFiltersToAggregationMetadata(@Nonnull final List<AggregationMetadata> originalMetadata, @Nullable final Filter filter) {
     if (filter == null) {
      return originalMetadata;
    }
    if (filter.hasOr()) {
      addOrFiltersToAggregationMetadata(filter.getOr(), originalMetadata);
    } else if (filter.hasCriteria()) {
      addCriteriaFiltersToAggregationMetadata(filter.getCriteria(), originalMetadata);
    }
    return originalMetadata;
  }

  void addOrFiltersToAggregationMetadata(@Nonnull final ConjunctiveCriterionArray or, @Nonnull final List<AggregationMetadata> originalMetadata) {
    for (ConjunctiveCriterion conjunction : or) {
      // For each item in the conjunction, inject an empty aggregation if necessary
      addCriteriaFiltersToAggregationMetadata(conjunction.getAnd(), originalMetadata);
    }
  }

  private void addCriteriaFiltersToAggregationMetadata(@Nonnull final CriterionArray criteria, @Nonnull final List<AggregationMetadata> originalMetadata) {
    for (Criterion criterion : criteria) {
      addCriterionFiltersToAggregationMetadata(criterion, originalMetadata);
    }
  }

  private void addCriterionFiltersToAggregationMetadata(
      @Nonnull final Criterion criterion,
      @Nonnull final List<AggregationMetadata> aggregationMetadata) {

    // We should never see duplicate aggregation for the same field in aggregation metadata list.
    final Map<String, AggregationMetadata> aggregationMetadataMap = aggregationMetadata.stream().collect(Collectors.toMap(
        AggregationMetadata::getName, agg -> agg));

    // Map a filter criterion to a facet field (e.g. domains.keyword -> domains)
    final String finalFacetField = toFacetField(criterion.getField());

    if (finalFacetField == null) {
      log.warn(String.format("Found invalid filter field for entity search. Invalid or unrecognized facet %s", criterion.getField()));
      return;
    }

    // We don't want to add urn filters to the aggregations we return as a sidecar to search results.
    // They are automatically added by searchAcrossLineage and we dont need them to show up in the filter panel.
    if (finalFacetField.equals(URN_FILTER)) {
      return;
    }

    if (aggregationMetadataMap.containsKey(finalFacetField)) {
      /*
       * If we already have aggregations for the facet field, simply inject any missing values counts into the set.
       * If there are no results for a particular facet value, it will NOT be in the original aggregation set returned by
       * Elasticsearch.
       */
      AggregationMetadata originalAggMetadata = aggregationMetadataMap.get(finalFacetField);
      if (criterion.hasValues()) {
        criterion.getValues().stream().forEach(value -> addMissingAggregationValueToAggregationMetadata(value, originalAggMetadata));
      } else {
        addMissingAggregationValueToAggregationMetadata(criterion.getValue(), originalAggMetadata);
      }
    } else {
      /*
       * If we do not have ANY aggregation for the facet field, then inject a new aggregation metadata object for the
       * facet field.
       * If there are no results for a particular facet, it will NOT be in the original aggregation set returned by
       * Elasticsearch.
       */
      aggregationMetadata.add(buildAggregationMetadata(
          finalFacetField,
          _filtersToDisplayName.getOrDefault(finalFacetField, finalFacetField),
          new LongMap(criterion.getValues().stream().collect(Collectors.toMap(i -> i, i -> 0L))),
          new FilterValueArray(criterion.getValues().stream().map(value -> createFilterValue(value, 0L, true)).collect(
              Collectors.toList())))
      );
    }
  }

  private void addMissingAggregationValueToAggregationMetadata(@Nonnull final String value, @Nonnull final AggregationMetadata originalMetadata) {
    if (
        originalMetadata.getAggregations().entrySet().stream().noneMatch(entry -> value.equals(entry.getKey()))
            || originalMetadata.getFilterValues().stream().noneMatch(entry -> entry.getValue().equals(value))
    ) {
      // No aggregation found for filtered value -- inject one!
      originalMetadata.getAggregations().put(value, 0L);
      originalMetadata.getFilterValues().add(createFilterValue(value, 0L, true));
    }
  }

  private AggregationMetadata buildAggregationMetadata(
      @Nonnull final String facetField,
      @Nonnull final String displayName,
      @Nonnull final LongMap aggValues,
      @Nonnull final FilterValueArray filterValues) {
    return new AggregationMetadata()
        .setName(facetField)
        .setDisplayName(displayName)
        .setAggregations(aggValues)
        .setFilterValues(filterValues);
  }

}
