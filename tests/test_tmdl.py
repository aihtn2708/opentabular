import pytest

from opentabular.tmdl import TmdlError, build_model, parse_tmdl

TABLE = """
/// Fact table.
/// One row per line.
table 'Sales Lines'
\tlineageTag: e9374b9a

\t/// Net revenue.
\tmeasure 'Sales Amount' = SUMX('Sales Lines', 'Sales Lines'[Quantity] * 'Sales Lines'[Net Price])
\t\tformatString: $ #,##0
\t\tdisplayFolder: " My ""Best"" Measures"

\tmeasure 'Sales (ly)' =
\t\t\tvar ly = SUM('Sales Lines'[Amount])

\t\t\treturn ly
\t\tformatString: $ #,##0

\tmeasure Fenced = ```
\t\t\tVAR x = 1
\t\t\t\tRETURN x
\t\t\t```
\t\tisHidden

\tcolumn 'Net Price'
\t\tdataType: int64
\t\tisHidden
\t\tisAvailableInMdx: false
\t\tsourceColumn: "Net Price"
\t\tsummarizeBy: none

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Doubled = 'Sales Lines'[Quantity] * 2
\t\tdataType: int64

\thierarchy Calendar
\t\tlevel Year
\t\t\tcolumn: Year

\tpartition 'Sales-Partition' = m
\t\tmode: import
\t\tsource =
\t\t\tlet
\t\t\t    Source = Sql.Database(Server, Database)
\t\t\tin
\t\t\t    Source
"""


def test_table_objects_and_properties():
    (table,) = parse_tmdl(TABLE)
    assert (table.type, table.name) == ("table", "Sales Lines")
    assert table.description == "Fact table.\nOne row per line."
    assert table.prop("lineageTag") == "e9374b9a"
    kinds = [(c.type, c.name) for c in table.children]
    assert kinds == [
        ("measure", "Sales Amount"), ("measure", "Sales (ly)"), ("measure", "Fenced"),
        ("column", "Net Price"), ("column", "Doubled"), ("hierarchy", "Calendar"),
        ("partition", "Sales-Partition"),
    ]


def test_expressions_single_multi_and_fenced():
    (table,) = parse_tmdl(TABLE)
    amount, ly, fenced = table.children[:3]
    assert amount.expr.startswith("SUMX('Sales Lines'")
    assert amount.description == "Net revenue."
    assert amount.prop("formatString") == "$ #,##0"
    assert amount.prop("displayFolder") == ' My "Best" Measures'
    # multi-line: blank line inside is kept, properties are not swallowed
    assert ly.expr == "var ly = SUM('Sales Lines'[Amount])\n\nreturn ly"
    assert ly.prop("formatString") == "$ #,##0"
    # fenced: the closing ``` sets the left edge, deeper indentation is kept
    assert fenced.expr == "VAR x = 1\n\tRETURN x"
    assert fenced.prop("isHidden") is True


def test_columns_partition_and_nested_objects():
    (table,) = parse_tmdl(TABLE)
    price = table.children[3]
    assert price.prop("isHidden") is True
    assert price.prop("isAvailableInMdx") == "false"
    assert price.prop("sourceColumn") == "Net Price"
    assert price.child("annotation").expr == "Automatic"
    assert table.children[4].expr == "'Sales Lines'[Quantity] * 2"
    assert table.children[5].child("level").prop("column") == "Year"
    part = table.children[6]
    assert part.expr == "m"
    assert part.prop("source").splitlines()[0] == "let"
    assert "    Source = Sql.Database(Server, Database)" in part.prop("source")


def test_build_model_relationships_and_entity_partition():
    text = TABLE + """
table Product
\tcolumn 'Product Key'
\t\tdataType: int64
\tpartition Product = entity
\t\tmode: directLake
\t\tsource
\t\t\tentityName: dim_product
\t\t\tschemaName: gold
\t\t\texpressionSource: DatabaseQuery

relationship cdb6e6a9-c9d1
\tfromColumn: 'Sales Lines'.'Net Price'
\ttoColumn: Product.'Product Key'
\tisActive: false

ref table Product
"""
    model = build_model(parse_tmdl(text))
    sales = model.table("sales lines")  # names are case-insensitive
    assert set(sales.measures) == {"sales amount", "sales (ly)", "fenced"}
    assert sales.column("net price").is_hidden
    assert sales.column("Doubled").expression is not None
    product = model.table("Product")
    assert (product.schema_name, product.entity_name) == ("gold", "dim_product")
    (rel,) = model.relationships
    assert (rel.from_table, rel.from_column) == ("Sales Lines", "Net Price")
    assert (rel.to_table, rel.to_column) == ("Product", "Product Key")
    assert rel.is_active is False
    assert model.structural_issues() == []


def test_spaces_indentation_and_script_wrapper():
    text = (
        "createOrReplace\n"
        "    table T\n"
        "        measure M = 1\n"
        "            formatString: 0\n"
        "        column C\n"
        "            dataType: int64\n"
    )
    (table,) = parse_tmdl(text)
    assert [c.name for c in table.children] == ["M", "C"]
    assert table.children[0].prop("formatString") == "0"


def test_partial_table_declarations_merge():
    model = build_model(parse_tmdl("table T\n\tcolumn A\n\ntable T\n\tmeasure M = 1\n"))
    t = model.table("T")
    assert list(t.columns) == ["a"] and list(t.measures) == ["m"]


def test_errors_carry_line_numbers():
    with pytest.raises(TmdlError, match=r"x\.tmdl:3"):
        parse_tmdl("table T\n\tcolumn A\n\t\t\t\tdataType: int64\n", "x.tmdl")
    with pytest.raises(TmdlError, match="Table.Column"):
        build_model(parse_tmdl("relationship r\n\tfromColumn: Sales\n\ttoColumn: P.K\n"))


def test_structural_issues_are_reported():
    model = build_model(parse_tmdl(
        "table A\n\tmeasure M = 1\n\tcolumn K\n\ntable B\n\tmeasure M = 2\n\n"
        "relationship r\n\tfromColumn: A.K\n\ttoColumn: B.Missing\n"
    ))
    issues = model.structural_issues()
    assert any("defined in both" in i for i in issues)
    assert any("'B'[Missing] does not exist" in i for i in issues)


def test_model_shaped_like_a_power_bi_export():
    """Annotations, variations, query groups, cultures with JSON metadata, M
    partitions and auto date tables must parse without disturbing the model.
    The fixture was written by hand in the shape Power BI exports."""
    from pathlib import Path

    from opentabular import load_model

    model = load_model(Path(__file__).parent / "fixtures" / "export_shape")
    sales = model.table("Sales")
    assert sales.measures["revenue"].expression == "SUM(Sales[Amount])"
    assert sales.measures["revenue"].format_string.startswith("\\$#,0.")
    assert set(sales.columns) == {"orderdate", "amount"}
    assert sales.partition_kind == "m" and sales.partition_source.startswith("let")
    auto = model.table("LocalDateTable_x")
    assert auto.is_hidden and auto.partition_kind == "calculated"
    assert auto.column("Year").expression == "YEAR([Date])"
    assert model.unsupported == ["calculated table 'LocalDateTable_x'"]
    assert [(r.from_table, r.to_table) for r in model.relationships] == [("Sales", "LocalDateTable_x")]
