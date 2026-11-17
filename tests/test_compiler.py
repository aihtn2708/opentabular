"""Compiler semantics, checked against values worked out by hand.

The `tiny` data set (see conftest.py):

    date        product  qty  price  channel   amount
    2023-01-15  A (1)      2    100  Online       200
    2023-02-10  B (2)      1    300  Store        300
    2023-02-20  C (3)      5     10  Online        50
    2023-07-01  A (1)      1    100  Store        100
    2024-01-20  A (1)      3    100  Online       300
    2024-02-10  C (3)     10     10  Store        100
    2024-02-29  B (2)      2    300  Online       600
    2024-08-05  ? (99)     1     50  Online        50   <- product key with no product

    Products: A Bikes/Red, B Bikes/Blue, C Parts/Red, D Parts/Blue (never sold)
    Totals: 2023 = 650, 2024 = 1050, all = 1700
"""
import pytest

from opentabular import CompileError, Query, UnsupportedError


def run(project, measures, group_by=(), filters=(), **kw):
    q = Query(list(measures), list(group_by), list(filters), **kw)
    return project.run(q, allow_adhoc_measures=True).rows


def one(project, measure, filters=()):
    ((value,),) = run(project, [measure], filters=filters)
    return value


# -- aggregation ------------------------------------------------------------

def test_grand_totals(tiny):
    assert one(tiny, "Total") == 1700
    assert one(tiny, "Qty") == 25
    assert one(tiny, "Avg Qty") == pytest.approx(25 / 8)
    assert one(tiny, "Products Sold") == 4          # keys 1, 2, 3, 99
    assert one(tiny, "Big Lines") == 2              # qty 5 and qty 10
    assert one(tiny, "Product Count") == 4


def test_group_by_dimension_includes_blank_member(tiny):
    # The sale with no matching product lands on a blank category, as in DAX.
    assert run(tiny, ["Total"], ["Product[Category]"]) == [("Bikes", 1500), ("Parts", 150), (None, 50)]


def test_group_by_two_tables(tiny):
    rows = run(tiny, ["Total"], ["'Date'[Year]", "Product[Category]"])
    assert rows == [
        (2023, "Bikes", 600), (2023, "Parts", 50),
        (2024, "Bikes", 900), (2024, "Parts", 100), (2024, None, 50),
    ]


def test_query_filters(tiny):
    assert one(tiny, "Total", ['Product[Category] = "Bikes"']) == 1500
    assert one(tiny, "Total", ['Product[Color] IN {"Red"}', "'Date'[Year] = 2024"]) == 400
    assert one(tiny, "Total", [{"column": "Sales[Channel]", "op": "!=", "value": "Online"}]) == 500
    assert one(tiny, "Total", [{"column": "'Date'[Year]", "op": "in", "value": [2023]}]) == 650
    assert one(tiny, "Total", ['NOT Product[Color] IN {"Red"}']) == 900
    assert one(tiny, "Total", ["'Date'[Date] >= DATE(2024, 2, 1)"]) == 750


def test_measure_on_unrelated_table_repeats(tiny):
    # Sales does not filter Product, so the product count is 4 on every row.
    assert run(tiny, ["Product Count"], ["Sales[Channel]"]) == [("Online", 4), ("Store", 4)]


def test_ad_hoc_measure(tiny):
    assert one(tiny, "Max Price = MAX(Sales[Price])") == 300
    assert one(tiny, "Net = [Total] * 0.9") == pytest.approx(1530)
    assert one(tiny, 'Label = IF([Total] > 1000, "high", "low")') == "high"
    assert one(tiny, 'Tier = SWITCH(TRUE(), [Total] > 5000, "A", [Total] > 1000, "B", "C")') == "B"
    assert one(tiny, "Related = SUMX(Sales, IF(RELATED(Product[Color]) = \"Red\", Sales[Qty], 0))") == 21


# -- CALCULATE --------------------------------------------------------------

def test_calculate_adds_a_filter(tiny):
    assert one(tiny, "Online") == 1200
    assert run(tiny, ["Online"], ["'Date'[Year]"]) == [(2023, 250), (2024, 950)]


def test_calculate_filter_replaces_filter_on_same_column(tiny):
    # Grouped by colour, the Color = "Red" filter overrides the row's colour.
    assert run(tiny, ["Red"], ["Product[Color]"]) == [("Blue", 750), ("Red", 750)]
    # KEEPFILTERS intersects instead, so only the Red row survives.
    assert run(tiny, ["Red Kept"], ["Product[Color]"]) == [("Red", 750)]
    # A filter on another column of the same table is kept.
    assert run(tiny, ["Red"], ["Product[Category]"]) == [("Bikes", 600), ("Parts", 150)]


def test_all_removes_filters_for_share_of_total(tiny):
    rows = run(tiny, ["Total", "Share of Products"], ["Product[Category]"])
    assert rows[0] == ("Bikes", 1500, pytest.approx(1500 / 1700))
    assert rows[1] == ("Parts", 150, pytest.approx(150 / 1700))
    assert rows[2] == (None, 50, pytest.approx(50 / 1700))     # blank member is kept
    # ALL(Product) also removes a query filter on Product.
    assert one(tiny, "Share of Products", ['Product[Category] = "Bikes"']) == pytest.approx(1500 / 1700)
    # ... but not a filter on another table.
    assert one(tiny, "Share of Products", ["'Date'[Year] = 2023"]) == pytest.approx(1.0)


def test_blank_is_zero_in_subtraction(tiny):
    rows = dict(((y, m), v) for y, m, v in run(tiny, ["Not Online"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert rows[(2023, 2)] == 300      # 350 total - 50 online
    assert rows[(2023, 7)] == 100      # 100 total - BLANK online
    assert rows[(2023, 1)] == 0        # 200 total - 200 online


def test_variable_is_evaluated_where_it_is_defined(tiny):
    assert one(tiny, "Red Var") == 750
    # Total is captured outside CALCULATE, so the inner filter does not touch it.
    dax = 'V = VAR t = [Total] RETURN CALCULATE(t, Sales[Channel] = "Online")'
    assert one(tiny, dax) == 1700


# -- time intelligence ------------------------------------------------------

def test_same_period_last_year(tiny):
    rows = run(tiny, ["Total", "LY", "YoY %"], ["'Date'[Year]"])
    assert rows == [(2023, 650, None, None), (2024, 1050, 650, pytest.approx(400 / 650))]
    by_month = dict(((y, m), v) for y, m, v in run(tiny, ["LY"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert by_month == {(2024, 1): 200, (2024, 2): 350, (2024, 7): 100}
    assert one(tiny, "LY", ["'Date'[Year] = 2024"]) == 650


def test_dateadd_previous_month(tiny):
    by_month = dict(((y, m), v) for y, m, v in run(tiny, ["Prev Month"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert by_month[(2023, 2)] == 200
    assert by_month[(2024, 3)] == 700
    assert (2023, 1) not in by_month


def test_year_to_date(tiny):
    by_month = dict(((y, m), v) for y, m, v in run(tiny, ["YTD"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert [by_month[(2023, m)] for m in (1, 2, 3, 6, 7, 12)] == [200, 550, 550, 550, 650, 650]
    assert [by_month[(2024, m)] for m in (1, 2, 7, 8, 12)] == [300, 1000, 1000, 1050, 1050]
    assert one(tiny, "YTD") == 1050                 # last date in the calendar is in 2024
    assert one(tiny, "YTD", ["'Date'[Year] = 2023"]) == 650


def test_year_to_date_last_year(tiny):
    by_month = dict(((y, m), v) for y, m, v in run(tiny, ["YTD LY"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert by_month[(2024, 1)] == 200
    assert by_month[(2024, 2)] == 550               # anchor 2024-02-29 shifts to 2023-02-28
    assert by_month[(2024, 12)] == 650
    assert (2023, 6) not in by_month


def test_rolling_window_and_running_total(tiny):
    rolling = dict(((y, m), v) for y, m, v in run(tiny, ["Rolling 3M"], ["'Date'[Year]", "'Date'[MonthNo]"]))
    assert rolling[(2023, 3)] == 550                # Jan + Feb 2023
    assert rolling[(2024, 2)] == 1000               # 2023-11-30 .. 2024-02-29
    assert rolling[(2024, 4)] == 700                # Feb 2024 only
    assert run(tiny, ["Cumulative"], ["'Date'[Year]"]) == [(2023, 650), (2024, 1700)]
    assert one(tiny, "Cumulative") == 1700


def test_time_intelligence_keeps_other_dimensions(tiny):
    rows = run(tiny, ["LY"], ["Product[Category]"], ["'Date'[Year] = 2024"])
    assert rows == [("Bikes", 600), ("Parts", 50)]
    assert run(tiny, ["All Dates"], ["'Date'[Year]"]) == [(2023, 1700), (2024, 1700)]


# -- level of detail: iterating the values of a column ----------------------

def test_iterating_column_values_evaluates_the_measure_per_value(tiny):
    # Bikes 1500 + Parts 150 + blank 50
    assert one(tiny, "Transition") == 1700
    assert run(tiny, ["Transition"], ["'Date'[Year]"]) == [(2023, 650), (2024, 1050)]
    # Monthly totals 2023: 200, 350, 100. 2024: 300, 700, 50.
    rows = run(tiny, ["M = AVERAGEX(VALUES('Date'[MonthNo]), [Total])"], ["'Date'[Year]"])
    assert rows == [(2023, pytest.approx(650 / 3)), (2024, pytest.approx(350))]
    # Across both years the month numbers are Jan 500, Feb 1050, Jul 100, Aug 50.
    assert one(tiny, "M = AVERAGEX(VALUES('Date'[MonthNo]), [Total])") == pytest.approx(425)
    assert one(tiny, "M = AVERAGEX(VALUES('Date'[MonthNo]), [Total])", ["'Date'[Year] = 2024"]) == pytest.approx(350)


def test_filter_over_column_values_and_row_value_access(tiny):
    # Products by revenue: A 600, B 900, C 150, D none, blank 50.
    assert one(tiny, "N = COUNTROWS(FILTER(VALUES(Product[Name]), [Total] > 500))") == 2
    assert one(tiny, "S = SUMX(FILTER(VALUES(Product[Name]), [Total] > 500), [Total])") == 1500
    rows = run(tiny, ["Best = MAXX(VALUES(Product[Name]), [Total])"], ["Product[Category]"])
    assert rows == [("Bikes", 900), ("Parts", 150), (None, 50)]
    # The iterated column is readable in the row expression.
    assert one(tiny, 'R = SUMX(VALUES(Product[Color]), IF(Product[Color] = "Red", [Total]))') == 750


def test_context_transition_needs_a_measure_or_calculate(tiny):
    # A bare aggregation inside the iterator is not filtered by the row: 25 for every category.
    assert one(tiny, "X = AVERAGEX(VALUES(Product[Category]), SUM(Sales[Qty]))") == 25
    # CALCULATE performs the transition: Bikes 9, Parts 15, blank 1.
    assert one(tiny, "X = MAXX(VALUES(Product[Category]), CALCULATE(SUM(Sales[Qty])))") == 15
    # CALCULATE filters apply after the transition, so ALL(Product) wins.
    assert one(tiny, "X = AVERAGEX(VALUES(Product[Category]), CALCULATE([Total], ALL(Product)))") == 1700
    assert one(tiny, 'X = AVERAGEX(VALUES(\'Date\'[Year]), CALCULATE([Total], Sales[Channel] = "Online"))') == 600


def test_summarize_and_all_as_iterator_sources(tiny):
    # Existing (category, year) pairs: Bikes and Parts in both years, blank in 2024.
    assert one(tiny, "N = COUNTROWS(SUMMARIZE(Sales, Product[Category], 'Date'[Year]))") == 5
    assert one(tiny, "X = MAXX(SUMMARIZE(Sales, 'Date'[Year], 'Date'[MonthNo]), [Total])") == 700
    # ALL(column) ignores the grouping on that column: the best category on every row.
    rows = run(tiny, ["Top = MAXX(ALL(Product[Category]), [Total])"], ["Product[Category]"])
    assert rows == [("Bikes", 1500), ("Parts", 1500)]


def test_level_of_detail_combines_with_time_intelligence(tiny):
    # Last year's revenue per category in 2024: Bikes 600, Parts 50.
    rows = run(tiny, ["X = AVERAGEX(VALUES(Product[Category]), [LY])"], ["'Date'[Year]"])
    assert rows == [(2024, pytest.approx(325))]


# -- query shape ------------------------------------------------------------

def test_order_and_limit(tiny):
    rows = run(tiny, ["Total"], ["Product[Name]"], order_by=["Total desc"], limit=2)
    assert rows == [("B", 900), ("A", 600)]


def test_dimension_only_query(tiny):
    assert run(tiny, [], ["Product[Category]"]) == [("Bikes",), ("Parts",)]


def test_output_names_disambiguate(tiny):
    q = Query(["Total"], ["Product[ProductKey]", "Sales[ProductKey]"])
    names = [c.name for c in tiny.compile(q).columns]
    assert names == ["Product[ProductKey]", "Sales[ProductKey]", "Total"]


# -- errors -----------------------------------------------------------------

@pytest.mark.parametrize("measures, group_by, filters, message", [
    (["Nope"], [], [], "unknown measure"),
    (["Total"], ["Product[Nope]"], [], "Unknown column"),
    (["Total"], [], ["Nope[x] = 1"], "unknown table"),
    (["Total"], [], ["1 = 1"], "condition on a column"),
    (["Loop A"], [], [], "circular reference"),
    (["X = Sales[Qty] * 2"], [], [], "without an aggregation"),
    (["X = SUM(Sales[Qty]"], [], [], "expected"),
    ([], [], [], "at least one"),
])
def test_invalid_queries(tiny, measures, group_by, filters, message):
    with pytest.raises(CompileError, match=message):
        tiny.compile(Query(measures, group_by, filters), allow_adhoc_measures=True)


@pytest.mark.parametrize("dax", [
    "X = SUMX(Sales, [Total])",                               # row-by-row context transition
    "X = SUMX(VALUES(Product[Category]), SUMX(VALUES(Product[Color]), [Total]))",
    "X = CALCULATE(AVERAGEX(VALUES('Date'[MonthNo]), [Total]), SAMEPERIODLASTYEAR('Date'[Date]))",
    "X = VAR t = [Total] RETURN SUMX(VALUES(Product[Category]), [Total] / t)",
    "X = CALCULATE([Total], USERELATIONSHIP(Sales[SaleDate], 'Date'[Date]))",
    "X = RANKX(ALL(Product), [Total])",
    "X = CALCULATE([YTD], DATESYTD('Date'[Date]))",           # to-date inside to-date
])
def test_unsupported_dax_is_reported_not_miscompiled(tiny, dax):
    with pytest.raises(UnsupportedError):
        tiny.compile(Query([dax]), allow_adhoc_measures=True)


def test_ad_hoc_measures_can_be_disabled(tiny):
    with pytest.raises(CompileError, match="ad-hoc measures are disabled"):
        tiny.compile(Query(["X = SUM(Sales[Qty])"]))
