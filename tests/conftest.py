"""Shared fixtures.

`tiny` is a hand-checkable project: eight sales rows whose totals are worked
out in comments in test_compiler.py. `sales` is the bundled example project.
"""
from pathlib import Path

import pytest

from opentabular import Project

ROOT = Path(__file__).resolve().parents[1]

TINY_TMDL = """
table Sales

\tmeasure Total = SUMX(Sales, Sales[Qty] * Sales[Price])
\tmeasure Qty = SUM(Sales[Qty])
\tmeasure 'Avg Qty' = AVERAGE(Sales[Qty])
\tmeasure Online = CALCULATE([Total], Sales[Channel] = "Online")
\tmeasure 'Not Online' = [Total] - [Online]
\tmeasure 'Share of Products' = DIVIDE([Total], CALCULATE([Total], ALL(Product)))
\tmeasure Red = CALCULATE([Total], Product[Color] = "Red")
\tmeasure 'Red Kept' = CALCULATE([Total], KEEPFILTERS(Product[Color] = "Red"))
\tmeasure 'Products Sold' = DISTINCTCOUNT(Sales[ProductKey])
\tmeasure 'Big Lines' = COUNTROWS(FILTER(Sales, Sales[Qty] >= 5))
\tmeasure LY = CALCULATE([Total], SAMEPERIODLASTYEAR('Date'[Date]))
\tmeasure 'YoY %' =
\t\t\tVAR Prev = [LY]
\t\t\tRETURN DIVIDE([Total] - Prev, Prev)
\tmeasure 'Prev Month' = CALCULATE([Total], DATEADD('Date'[Date], -1, MONTH))
\tmeasure YTD = TOTALYTD([Total], 'Date'[Date])
\tmeasure 'YTD LY' = CALCULATE([YTD], SAMEPERIODLASTYEAR('Date'[Date]))
\tmeasure 'Rolling 3M' = CALCULATE([Total], DATESINPERIOD('Date'[Date], MAX('Date'[Date]), -3, MONTH))
\tmeasure Cumulative = CALCULATE([Total], FILTER(ALL('Date'), 'Date'[Date] <= MAX('Date'[Date])))
\tmeasure 'All Dates' = CALCULATE([Total], ALL('Date'))
\tmeasure 'Product Count' = COUNTROWS(Product)
\tmeasure 'Red Var' =
\t\t\tVAR c = "Red"
\t\t\tRETURN CALCULATE([Total], Product[Color] = c)
\tmeasure 'Loop A' = [Loop B] + 1
\tmeasure 'Loop B' = [Loop A] + 1
\tmeasure Transition = SUMX(VALUES(Product[Category]), [Total])

\tcolumn SaleDate
\t\tdataType: dateTime
\t\tsourceColumn: sale_date
\tcolumn ProductKey
\t\tdataType: int64
\t\tsourceColumn: product_key
\tcolumn Qty
\t\tdataType: int64
\t\tsourceColumn: qty
\tcolumn Price
\t\tdataType: double
\t\tsourceColumn: price
\tcolumn Channel
\t\tdataType: string
\t\tsourceColumn: channel

table Product

\tcolumn ProductKey
\t\tdataType: int64
\t\tsourceColumn: product_key
\tcolumn Name
\t\tdataType: string
\t\tsourceColumn: name
\tcolumn Category
\t\tdataType: string
\t\tsourceColumn: category
\tcolumn Color
\t\tdataType: string
\t\tsourceColumn: color

table Date

\tcolumn Date
\t\tdataType: dateTime
\t\tsourceColumn: d
\tcolumn Year
\t\tdataType: int64
\t\tsourceColumn: y
\tcolumn MonthNo
\t\tdataType: int64
\t\tsourceColumn: m

relationship r1
\tfromColumn: Sales.ProductKey
\ttoColumn: Product.ProductKey

relationship r2
\tfromColumn: Sales.SaleDate
\ttoColumn: Date.Date
"""

SALES_CSV = """sale_date,product_key,qty,price,channel
2023-01-15,1,2,100,Online
2023-02-10,2,1,300,Store
2023-02-20,3,5,10,Online
2023-07-01,1,1,100,Store
2024-01-20,1,3,100,Online
2024-02-10,3,10,10,Store
2024-02-29,2,2,300,Online
2024-08-05,99,1,50,Online
"""

PRODUCT_CSV = """product_key,name,category,color
1,A,Bikes,Red
2,B,Bikes,Blue
3,C,Parts,Red
4,D,Parts,Blue
"""


@pytest.fixture(scope="session")
def tiny(tmp_path_factory) -> Project:
    root = tmp_path_factory.mktemp("tiny")
    (root / "definition").mkdir()
    (root / "definition" / "model.tmdl").write_text(TINY_TMDL)
    (root / "sales.csv").write_text(SALES_CSV)
    (root / "product.csv").write_text(PRODUCT_CSV)
    import datetime as dt

    lines = ["d,y,m"]
    day = dt.date(2023, 1, 1)
    while day <= dt.date(2024, 12, 31):
        lines.append(f"{day.isoformat()},{day.year},{day.month}")
        day += dt.timedelta(days=1)
    (root / "date.csv").write_text("\n".join(lines) + "\n")
    (root / "opentabular.yml").write_text(
        "model: definition\n"
        "sources:\n  Sales: sales.csv\n  Product: product.csv\n  Date: date.csv\n"
    )
    return Project.load(root)


@pytest.fixture(scope="session")
def sales() -> Project:
    return Project.load(ROOT / "examples" / "sales")
