"""Download the UCI Online Retail dataset (541,909 transactions, 45 MB) into data/.

Original source: Chen, D. (2015). Online Retail. UCI Machine Learning Repository.
https://doi.org/10.24432/C5BW33  (licence: CC BY 4.0)
The CSV copy used here is the mirror in the Databricks "Spark: The Definitive Guide" repo.
"""
from pathlib import Path
from urllib.request import urlretrieve

URL = ("https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/"
       "master/data/retail-data/all/online-retail-dataset.csv")
dest = Path(__file__).resolve().parents[1] / "data" / "online_retail.csv"
dest.parent.mkdir(exist_ok=True)
if dest.exists():
    print("already downloaded:", dest)
else:
    print("downloading ..."); urlretrieve(URL, dest); print("saved to", dest)
