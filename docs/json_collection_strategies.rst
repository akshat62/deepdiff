JSON Collection Strategies
==========================

``DeepJSONDiff`` is an opt-in facade for comparing JSON-like values whose nested
arrays require path-specific semantics. It canonicalizes both inputs without
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

Array wildcards select exactly one array index under every matching parent.
Object-key wildcards likewise select exactly one key. Relative identity fields
may be nested and composite::

    CollectionStrategy(
        path="$.orders[*].items",
        match_by=("product.id", "warehouse.code"),
    )

    CollectionStrategy(
        path="$.*.users",
        match_by=("id",),
    )

Identity fields must resolve to JSON scalar values: ``None``, booleans,
integers, finite floats, or strings. Structured values such as dictionaries and
lists are rejected because they do not provide a stable business identity.

Filtering
---------

``filter_func`` is applied symmetrically to defensive copies of both inputs
before matching and comparison. A filter may therefore inspect or mutate the
value it receives without changing caller-owned payloads. Filter counts are
available through ``get_stats()``::

    strategy = CollectionStrategy(
        path="$.users",
        match_by=("id",),
        filter_func=lambda item: item.get("active") is True,
    )

Sorting and order-insensitive arrays
------------------------------------

``sort_by`` creates deterministic ordering for arrays where identity matching is
not required. Numeric values use numeric ordering; missing, null, mixed, and
structured values use a stable type-aware order.

``compare_as_set`` performs multiset (bag) comparison for scalar arrays. Order
is ignored but duplicate counts remain significant::

    CollectionStrategy(path="$.events", sort_by=("timestamp", "id"))
    CollectionStrategy(path="$.roles", compare_as_set=True)

``compare_as_set`` cannot be combined with ``match_by`` or ``sort_by``.

Missing identities
------------------

The default ``MissingIdentityPolicy.FALLBACK`` retains records lacking one or
more identity fields and compares them using their relative order. Strict and
exclusion policies are also available. Enum members and their string values are
accepted::

    from deepdiff import MissingIdentityPolicy

    CollectionStrategy(
        path="$.items",
        match_by=("id",),
        missing_identity=MissingIdentityPolicy.ERROR,
    )

    CollectionStrategy(
        path="$.items",
        match_by=("id",),
        missing_identity="exclude",
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

Duplicate identities are encoded with a canonical, type-preserving format, so
distinct composite identities cannot collide.

Normalization and volatile fields
---------------------------------

Normalizers run before identity extraction and comparison. Each selected item
is deep-copied before filters and normalizers run, so callbacks may mutate their
argument without mutating the caller's input. ``exclude_fields`` removes
volatile top-level fields from each selected array item::

    CollectionStrategy(
        path="$.events",
        match_by=("eventId",),
        normalizers=(normalize_region,),
        exclude_fields=("requestId", "generatedAt"),
    )

Rule precedence
---------------

When multiple rules match a concrete path, higher ``priority`` wins. At equal
priority, the pattern with more exact tokens wins. Equally specific matches are
rejected as ambiguous instead of being resolved silently.

Diagnostics
-----------

``get_stats()`` returns per-concrete-path execution information, including
input counts, filtered counts, missing identities, duplicate groups, and the
selected strategy name.

Result interface
----------------

``DeepJSONDiff`` implements the standard read-only mapping interface and
delegates serialization to the underlying ``DeepDiff`` result::

    if diff:
        print(diff["values_changed"])

    for category, changes in diff.items():
        print(category, changes)

    print(diff.to_json(sort_keys=True))

Compatibility
-------------

``DeepJSONDiff`` is separate from ``DeepDiff`` by design. Existing constructor
parameters, result views, Delta behaviour, iterable callbacks, and
multiprocessing paths are not modified. Keyword arguments unrelated to
collection strategies are forwarded to the underlying ``DeepDiff`` instance.
