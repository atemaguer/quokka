import sys
sys.path.append("/home/ubuntu/quokka/pyquokka")
import time
from pyquokka.quokka_runtime import TaskGraph
from pyquokka.sql import AggExecutor, PolarJoinExecutor, StorageExecutor
from pyquokka.dataset import InputMultiParquetDataset
import ray
import polars
import pyarrow as pa
import pyarrow.compute as compute
import redis
r = redis.Redis(host="localhost", port=6800, db=0)
r.flushall()

ips = ['localhost', '172.31.11.134', '172.31.15.208', '172.31.11.188']
workers = 1

task_graph = TaskGraph()

def batch_func(df):
    df["high"] = ((df["o_orderpriority"] == "1-URGENT") | (df["o_orderpriority"] == "2-HIGH")).astype(int)
    df["low"] = ((df["o_orderpriority"] != "1-URGENT") & (df["o_orderpriority"] != "2-HIGH")).astype(int)
    result = df.groupby("l_shipmode").agg({'high':['sum'],'low':['sum']})
    return result

lineitem_scheme = ["l_orderkey","l_partkey","l_suppkey","l_linenumber","l_quantity","l_extendedprice", 
"l_discount","l_tax","l_returnflag","l_linestatus","l_shipdate","l_commitdate","l_receiptdate","l_shipinstruct",
"l_shipmode","l_comment", "null"]
order_scheme = ["o_orderkey", "o_custkey","o_orderstatus","o_totalprice","o_orderdate","o_orderpriority","o_clerk",
"o_shippriority","o_comment", "null"]

orders_filter = lambda x: polars.from_arrow(x.select(["o_orderkey","o_orderpriority"]))
lineitem_filter = lambda x: polars.from_arrow(x.filter(compute.and_(compute.and_(compute.and_(compute.is_in(x["l_shipmode"],value_set = pa.array(["SHIP","MAIL"])), compute.less(x["l_commitdate"], x["l_receiptdate"])), compute.and_(compute.less(x["l_shipdate"], x["l_commitdate"]), compute.greater_equal(x["l_receiptdate"], compute.strptime("1994-01-01",format="%Y-%m-%d",unit="s")))), compute.less(x["l_receiptdate"], compute.strptime("1995-01-01",format="%Y-%m-%d",unit="s")))).select(["l_orderkey","l_shipmode"]))
orders_filter_parquet = lambda x: polars.from_arrow(x)
lineitem_filter_parquet = lambda x: polars.from_arrow(x.filter(compute.and_(compute.less(x["l_commitdate"], x["l_receiptdate"]), compute.less(x["l_shipdate"], x["l_commitdate"]))).select(["l_orderkey","l_shipmode"]))

def partition_key1(data, source_channel, target_channel):

    if source_channel // 8 == target_channel:
        return data
    else:
        return None

lineitem_parquet_reader = InputMultiParquetDataset("tpc-h-parquet","lineitem.parquet",columns=['l_shipdate','l_commitdate','l_shipmode','l_receiptdate','l_orderkey'], filters= [('l_shipmode', 'in', ['SHIP','MAIL']),('l_receiptdate','<',compute.strptime("1995-01-01",format="%Y-%m-%d",unit="s")), ('l_receiptdate','>=',compute.strptime("1994-01-01",format="%Y-%m-%d",unit="s"))])
orders_parquet_reader = InputMultiParquetDataset("tpc-h-parquet","orders.parquet",columns = ['o_orderkey','o_orderpriority'])

lineitem = task_graph.new_input_reader_node(lineitem_parquet_reader, {ip:8 for ip in ips[:workers]}, batch_func = lineitem_filter_parquet)
orders = task_graph.new_input_reader_node(orders_parquet_reader, {ip:8 for ip in ips[:workers]}, batch_func = orders_filter_parquet)

storage = StorageExecutor()
cached_lineitem = task_graph.new_blocking_node({0:lineitem},None, storage, {ip:1 for ip in ips[:workers]}, {0:partition_key1})
cached_orders = task_graph.new_blocking_node({0:orders},None, storage, {ip:1 for ip in ips[:workers]}, {0:partition_key1})


task_graph.create()
start = time.time()
task_graph.run_with_fault_tolerance()
load_time = time.time() - start

task_graph2 = TaskGraph()
lineitem = task_graph2.new_input_redis(cached_lineitem, {ip:4 for ip in ips[:workers]})
orders = task_graph2.new_input_redis(cached_orders, {ip:4 for ip in ips[:workers]})

join_executor = PolarJoinExecutor(left_on="o_orderkey",right_on="l_orderkey", batch_func=batch_func)
cached_joined = task_graph2.new_blocking_node({0:orders,1:lineitem},None,join_executor, {ip:4 for ip in ips[:workers]}, {0:"o_orderkey", 1:"l_orderkey"})

task_graph2.create()
start = time.time()
task_graph2.run_with_fault_tolerance()
compute_time = time.time() - start

task_graph3 = TaskGraph()
joined = task_graph3.new_input_redis(cached_joined, {ip:4 for ip in ips[:workers]})
agg_executor = AggExecutor()
agged = task_graph3.new_blocking_node({0:joined}, None, agg_executor, {'localhost':1}, {0:None})

task_graph3.create()
start = time.time()
task_graph3.run_with_fault_tolerance()
compute_time += time.time() - start

print("load time ", load_time)
print("compute time ", compute_time)

print(ray.get(agged.to_pandas.remote()))
