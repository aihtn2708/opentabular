"""Differential tests on the example project: each semantic query is compared
with SQL written by hand in a different style (window functions, correlated
sub-queries) straight against the source tables."""
import pytest

from opentabular import Query

AMOUNT = "s.quantity * s.unit_price * (1 - s.discount)"


def semantic(project, measures, group_by=(), filters=()):
    return project.run(Query(list(measures), list(group_by), list(filters))).rows


def sql(project, text):
    return project.connect().execute(text).fetchall()


def assert_same(got, want):
    assert len(got) == len(want), (len(got), len(want))
    for g, w in zip(got, want):
        assert len(g) == len(w)
        for a, b in zip(g, w):
            if isinstance(a, float) or isinstance(b, float):
                assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (g, w)
            else:
                assert a == b, (g, w)


def test_revenue_cost_and_margin_by_category(sales):
    got = semantic(sales, ["Total Sales", "Total Cost", "Gross Margin", "Gross Margin %", "Total Quantity"],
                   ["Product[Category]"])
    want = sql(sales, f"""
        SELECT p.category, SUM({AMOUNT}), SUM(s.quantity * p.unit_cost),
               SUM({AMOUNT}) - SUM(s.quantity * p.unit_cost),
               (SUM({AMOUNT}) - SUM(s.quantity * p.unit_cost)) / SUM({AMOUNT}),
               SUM(s.quantity)
        FROM sales s JOIN product p USING (product_key)
        GROUP BY 1 ORDER BY 1""")
    assert_same(got, want)


def test_distinct_counts_and_average_order_value_by_country_and_year(sales):
    got = semantic(sales, ["Orders", "Customers", "Average Order Value"],
                   ["Customer[Country]", "'Date'[Year]"])
    want = sql(sales, f"""
        SELECT c.country, year(s.order_date), COUNT(DISTINCT s.order_id), COUNT(DISTINCT s.customer_key),
               SUM({AMOUNT}) / COUNT(DISTINCT s.order_id)
        FROM sales s JOIN customer c USING (customer_key)
        GROUP BY 1, 2 ORDER BY 1, 2""")
    assert_same(got, want)


def test_online_share_and_share_of_all_products(sales):
    got = semantic(sales, ["Online Sales", "Online Share %", "% of All Products"], ["Product[Product]"],
                   ['Customer[Segment] = "Corporate"'])
    want = sql(sales, f"""
        WITH f AS (
            SELECT p.product_name, s.channel, {AMOUNT} AS amt
            FROM sales s JOIN product p USING (product_key) JOIN customer c USING (customer_key)
            WHERE c.segment = 'Corporate')
        SELECT product_name,
               SUM(amt) FILTER (WHERE channel = 'Online'),
               SUM(amt) FILTER (WHERE channel = 'Online') / SUM(amt),
               SUM(amt) / (SELECT SUM(amt) FROM f)
        FROM f GROUP BY 1 ORDER BY 1""")
    assert_same(got, want)


def test_same_period_last_year_by_month(sales):
    got = semantic(sales, ["Total Sales", "Sales LY", "Sales YoY %"], ["'Date'[Year Month]"],
                   ["'Date'[Year] = 2024"])
    want = sql(sales, f"""
        WITH m AS (
            SELECT strftime(s.order_date, '%Y-%m') AS ym, SUM({AMOUNT}) AS amt FROM sales s GROUP BY 1)
        SELECT cur.ym, cur.amt, prev.amt, (cur.amt - prev.amt) / prev.amt
        FROM m cur LEFT JOIN m prev
          ON prev.ym = (CAST(left(cur.ym, 4) AS INT) - 1) || right(cur.ym, 3)
        WHERE cur.ym LIKE '2024%' ORDER BY 1""")
    assert_same(got, want)


def test_year_to_date_by_month_uses_calendar_not_sales_months(sales):
    got = semantic(sales, ["Sales YTD", "Sales YTD LY"], ["'Date'[Year Month]"], ["'Date'[Year] = 2024"])
    want = sql(sales, f"""
        WITH daily AS (
            SELECT d.date, d.year_month, COALESCE(SUM({AMOUNT}), 0) AS amt
            FROM date d LEFT JOIN sales s ON s.order_date = d.date GROUP BY 1, 2),
        run AS (
            SELECT date, year_month,
                   SUM(amt) OVER (PARTITION BY year(date) ORDER BY date) AS ytd FROM daily),
        month_end AS (SELECT year_month, MAX(date) AS last_day FROM run GROUP BY 1)
        SELECT me.year_month,
               (SELECT ytd FROM run WHERE date = me.last_day),
               (SELECT ytd FROM run WHERE date = CAST(me.last_day - INTERVAL 1 YEAR AS DATE))
        FROM month_end me WHERE me.year_month LIKE '2024%' ORDER BY 1""")
    assert_same(got, want)


def test_rolling_three_months_and_running_total_by_month(sales):
    got = semantic(sales, ["Sales Rolling 3M", "Cumulative Sales"], ["'Date'[Year Month]"])
    want = sql(sales, f"""
        WITH month_end AS (SELECT year_month, MAX(date) AS last_day FROM date GROUP BY 1)
        SELECT me.year_month,
               (SELECT SUM({AMOUNT}) FROM sales s
                 WHERE s.order_date <= me.last_day
                   AND s.order_date > CAST(me.last_day - INTERVAL 3 MONTH AS DATE)),
               (SELECT SUM({AMOUNT}) FROM sales s WHERE s.order_date <= me.last_day)
        FROM month_end me ORDER BY 1""")
    assert_same(got, want)


def test_time_intelligence_with_a_second_dimension(sales):
    got = semantic(sales, ["Sales LY", "Sales YTD"], ["Product[Category]", "'Date'[Quarter]"],
                   ["'Date'[Year] = 2024", 'Sales[Channel] = "Store"'])
    want = sql(sales, f"""
        WITH f AS (
            SELECT p.category, s.order_date, {AMOUNT} AS amt
            FROM sales s JOIN product p USING (product_key) WHERE s.channel = 'Store'),
        cells AS (
            SELECT c.category, q.quarter, q.first_day, q.last_day
            FROM (SELECT DISTINCT category FROM product) c
            CROSS JOIN (SELECT quarter, MIN(date) AS first_day, MAX(date) AS last_day
                        FROM date WHERE year = 2024 GROUP BY 1) q)
        SELECT * FROM (
            SELECT category, quarter,
                   (SELECT SUM(amt) FROM f WHERE f.category = cells.category
                      AND f.order_date BETWEEN CAST(first_day - INTERVAL 1 YEAR AS DATE)
                                           AND CAST(last_day - INTERVAL 1 YEAR AS DATE)) AS ly,
                   (SELECT SUM(amt) FROM f WHERE f.category = cells.category
                      AND f.order_date BETWEEN DATE '2024-01-01' AND last_day) AS ytd
            FROM cells)
        WHERE ly IS NOT NULL OR ytd IS NOT NULL ORDER BY 1, 2""")
    assert_same(got, want)


def test_every_supported_example_measure_compiles_and_runs(sales):
    names = [m.name for m in sales.model.all_measures() if m.name != "Top Product Names"]
    rows = semantic(sales, names, ["'Date'[Year]"])
    assert [r[0] for r in rows] == [2023, 2024]
    assert all(v is not None for v in rows[1][1:])


def test_level_of_detail_average_and_best_month_by_category(sales):
    got = semantic(sales, ["Avg Monthly Sales", "Best Month Sales"], ["Product[Category]"],
                   ['Customer[Country] = "Vietnam"'])
    want = sql(sales, f"""
        WITH monthly AS (
            SELECT p.category, strftime(s.order_date, '%Y-%m') AS ym, SUM({AMOUNT}) AS amt
            FROM sales s JOIN product p USING (product_key) JOIN customer c USING (customer_key)
            WHERE c.country = 'Vietnam' GROUP BY 1, 2)
        SELECT category, AVG(amt), MAX(amt) FROM monthly GROUP BY 1 ORDER BY 1""")
    assert_same(got, want)


def test_level_of_detail_count_of_customers_over_a_threshold_by_year(sales):
    got = semantic(sales, ["Customers Over 10K"], ["'Date'[Year]"])
    want = sql(sales, f"""
        SELECT y, COUNT(*) FROM (
            SELECT year(s.order_date) AS y, s.customer_key, SUM({AMOUNT}) AS amt
            FROM sales s GROUP BY 1, 2 HAVING SUM({AMOUNT}) > 10000)
        GROUP BY 1 ORDER BY 1""")
    assert_same(got, want)


def test_rank_and_top_three_products_by_year(sales):
    got = semantic(sales, ["Total Sales", "Product Rank"], ["'Date'[Year]", "Product[Product]"])
    want = sql(sales, f"""
        SELECT year(s.order_date), p.product_name, SUM({AMOUNT}),
               RANK() OVER (PARTITION BY year(s.order_date) ORDER BY SUM({AMOUNT}) DESC)
        FROM sales s JOIN product p USING (product_key) GROUP BY 1, 2 ORDER BY 1, 2""")
    assert_same(got, want)
    got = semantic(sales, ["Top 3 Products Sales"], ["Customer[Country]"])
    want = sql(sales, f"""
        SELECT country, SUM(amt) FROM (
            SELECT c.country, s.product_key, SUM({AMOUNT}) AS amt,
                   ROW_NUMBER() OVER (PARTITION BY c.country ORDER BY SUM({AMOUNT}) DESC) AS rn
            FROM sales s JOIN customer c USING (customer_key) GROUP BY 1, 2)
        WHERE rn <= 3 GROUP BY 1 ORDER BY 1""")
    assert_same(got, want)


def test_row_by_row_average_previous_month_and_calculated_column(sales):
    got = semantic(sales, ["Avg Sales per Customer"], ["Product[Category]", "'Date'[Year]"])
    want = sql(sales, f"""
        SELECT category, y, AVG(amt) FROM (
            SELECT p.category, year(s.order_date) AS y, s.customer_key, SUM({AMOUNT}) AS amt
            FROM sales s JOIN product p USING (product_key) GROUP BY 1, 2, 3)
        GROUP BY 1, 2 ORDER BY 1, 2""")
    assert_same(got, want)
    got = semantic(sales, ["Sales Previous Month"], ["'Date'[Year Month]"])
    want = sql(sales, f"""
        WITH m AS (SELECT date_trunc('month', s.order_date) AS mo, SUM({AMOUNT}) AS amt FROM sales s GROUP BY 1)
        SELECT strftime(d.mo, '%Y-%m'), m.amt
        FROM (SELECT DISTINCT date_trunc('month', date) AS mo FROM date) d
        JOIN m ON m.mo = d.mo - INTERVAL 1 MONTH ORDER BY 1""")
    assert_same(got, want)
    got = sales.run(__import__("opentabular").Query(
        ["S = SUM(Sales[Line Amount])", "Total Sales"], ["Sales[Channel]"]), True).rows
    assert all(a == pytest.approx(b) for _, a, b in got) and len(got) == 2


def test_row_level_security_matches_a_filtered_query(sales):
    from opentabular import Query
    from opentabular.security import Identity

    got = sales.run(Query(["Total Sales", "Orders", "Sales LY", "Product Rank"], ["Product[Category]"]),
                    identity=Identity("viewer", ["Vietnam Sales"])).rows
    want = sales.run(Query(["Total Sales", "Orders", "Sales LY", "Product Rank"], ["Product[Category]"],
                           ['Customer[Country] = "Vietnam"'])).rows
    assert_same(got, want)
