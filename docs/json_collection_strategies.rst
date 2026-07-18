JSON Collection Strategies
==========================

``DeepJSONDiff`` is an opt-in facade for comparing JSON-like values whose nested
arrays require path-specific semantics. It canonicalizes the two inputs without
mutating them and delegates the final recursive comparison to ``DeepDiff``.
Existing ``DeepDiff`` behaviour is unchanged.

Basic identity matching
-----------------------

Use ``match_by`` when a list contains records with a stable business identity.
Reordering does not create differences, while additions, removals and field
changes remain visible::

    from deepdiff import CollectionStrategy, DeepJSONDiff

    diff = DeepJSONDiff(
        expected,
        actual,
        collection_strategies=[
            CollectionStrategy(
                path="$.users",
                match_by=("id",),
            )
        ],
    )

Nested arrays and composite keys
--------------------------------

Array wildcards select the same collection under every matching parent. Relative
identity fields may be nested and composite::

    CollectionStrategy(
        path="$.orders[*].items",
        match_by=("product.id", "warehouse.code"),
    )

Filtering
---------

``filter_func`` is applied symmetrically to both inputs before matching and
comparison. Filter counts are available through ``get_stats()``::

    strategy = CollectionStrategy(
        path="$.users",
        match_by=("id",),
        filter_func=lambda item: item.get("active") is True,
    )

Sorting and set-like arrays
---------------------------

``sort_by`` creates deterministic ordering for arrays where identity matching is
not required. ``compare_as_set`` is suitable for primitive arrays where order is
not meaningful; duplicate values remain significant::

    CollectionStrategy(path="$.events", sort_by=("timestamp", "id"))
    CollectionStrategy(path="$.roles", compare_as_set=True)

Missing identities
------------------

The default ``MissingIdentityPolicy.FALLBACK`` retains records lacking one or
more identity fields and compares them using their relative order. Strict and
exclusion policies are also available::

    from deepdiff import MissingIdentityPolicy

    CollectionStrategy(
        path="$.items",
        match_by=("id",),
        missing_identity=MissingIdentityPolicy.ERROR,
    )

Duplicate identities
--------------------

Duplicate identities raise ``DuplicateIdentityError`` by default. To compare a
duplicate group, use ``DuplicateIdentityPolicy.GROUP`` and preferably provide a
secondary ``sort_by`` key::

    from deepdiff import DuplicateIdentityPolicy

    CollectionStrategy(
        path="$.items",
        match_by=("id",),
        sort_by=("version",),
        duplicates=DuplicateIdentityPolicy.GROUP,
    )

Normalization and volatile fields
---------------------------------

Normalizers run before identity extraction and comparison. ``exclude_fields``
removes volatile top-level fields from each selected array item. Neither feature
mutates the caller's input::

    CollectionStrategy(
        path="$.events",
        match_by=("eventId",),
        normalizers=(normalize_region,),
        exclude_fields=("requestId", "generatedAt"),
    )

Rule precedence
---------------

When multiple rules match a concrete path, higher ``priority`` wins. At equal
priority, the longer path pattern wins. Equally specific matches are rejected as
ambiguous instead of being resolved silently.

Compatibility
-------------

``DeepJSONDiff`` is separate from ``DeepDiff`` by design. Existing constructor
parameters, result views, Delta behaviour, iterable callbacks and multiprocessing
paths are not modified. Keyword arguments not related to collection strategies
are forwarded to the underlying ``DeepDiff`` instance.
