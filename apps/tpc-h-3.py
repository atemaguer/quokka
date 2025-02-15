import sys
import time
from pyquokka.quokka_runtime import TaskGraph
from pyquokka.sql import AggExecutor, PolarJoinExecutor
from pyquokka.dataset import InputCSVDataset, InputMultiParquetDataset
from schema import * 
import pyarrow.compute as compute
import polars
import redis
import ray
r = redis.Redis(host="localhost", port=6800, db=0)
r.flushall()
task_graph = TaskGraph()

# aggregation push down might be interesting to think about
# this is not very good because we don't know the thing is actually sorted in l_order on lineitem

ips = [ 'localhost','172.31.4.47', '172.31.4.177', '172.31.10.39', '172.31.8.11', '172.31.10.151', '172.31.13.215', '172.31.8.52', '172.31.9.247', '172.31.4.185', '172.31.4.25', '172.31.11.56', '172.31.11.153', '172.31.13.223', '172.31.6.251', '172.31.3.59']
workers = 1

def batch_func2(df):
    df["product"] = df["l_extendedprice"] * (1 - df["l_discount"])
    return df.groupby(["o_orderkey", "o_orderdate", "o_shippriority"]).agg(revenue = ('product','sum'))

def final_func(state):
    return state.sort_values(['revenue','o_orderdate'],ascending = [False,True])[:10]

orders_filter = lambda x: polars.from_arrow(x.filter(compute.less(x['o_orderdate'] , compute.strptime("1995-03-03",format="%Y-%m-%d",unit="s"))).select(["o_orderkey","o_custkey","o_shippriority", "o_orderdate"]))
lineitem_filter = lambda x: polars.from_arrow(x.filter(compute.greater(x['l_shipdate'] , compute.strptime("1995-03-15",format="%Y-%m-%d",unit="s"))).select(["l_orderkey","l_extendedprice","l_discount"]))
customer_filter = lambda x: polars.from_arrow(x.filter(compute.equal(x["c_mktsegment"] , 'BUILDING')).select(["c_custkey"]))

if sys.argv[1] == "csv":
    
    lineitem_csv_reader = InputCSVDataset("tpc-h-csv", "lineitem/lineitem.tbl.1", lineitem_scheme , sep="|", stride = 128 * 1024 * 1024)
    orders_csv_reader = InputCSVDataset("tpc-h-csv", "orders/orders.tbl.1", order_scheme , sep="|", stride = 128 * 1024 * 1024)
    customer_csv_reader = InputCSVDataset("tpc-h-csv", "customer/customer.tbl.1", customer_scheme , sep="|", stride = 128 * 1024 * 1024)

    lineitem = task_graph.new_input_reader_node(lineitem_csv_reader,{ips[i]: 8 for i in range(workers)}, batch_func = lineitem_filter)
    orders = task_graph.new_input_reader_node(orders_csv_reader, {ips[i]: 4 for i in range(workers)}, batch_func = orders_filter)
    customer = task_graph.new_input_reader_node(customer_csv_reader,{ips[i]: 4 for i in range(workers)}, batch_func = customer_filter)

else:

    lineitem_parquet_reader = InputMultiParquetDataset("tpc-h-parquet","lineitem.parquet", columns = ["l_orderkey","l_extendedprice","l_discount"], filters= [('l_shipdate','>',compute.strptime("1995-03-15",format="%Y-%m-%d",unit="s"))])
    orders_parquet_reader =InputMultiParquetDataset("tpc-h-parquet","orders.parquet", columns = ["o_orderkey","o_custkey","o_shippriority", "o_orderdate"], filters= [('o_orderdate','<',compute.strptime("1995-03-03",format="%Y-%m-%d",unit="s"))])
    customer_parquet_reader = InputMultiParquetDataset("tpc-h-parquet","customer.parquet", columns = ["c_custkey"], filters= [("c_mktsegment","==","BUILDING")])

    lineitem = task_graph.new_input_reader_node(lineitem_parquet_reader, {ips[i]: 8 for i in range(workers)})
    orders = task_graph.new_input_reader_node(orders_parquet_reader,{ips[i]: 8 for i in range(workers)})
    customer = task_graph.new_input_reader_node(customer_parquet_reader, {ips[i]: 8 for i in range(workers)})
    

# join order picked by hand, might not be  the best one!
join_executor1 = PolarJoinExecutor(left_on = "c_custkey", right_on = "o_custkey",columns=["o_orderkey", "o_orderdate", "o_shippriority"])
join_executor2 = PolarJoinExecutor(left_on="o_orderkey",right_on="l_orderkey",batch_func=batch_func2)
temp = task_graph.new_non_blocking_node({0:customer,1:orders},None, join_executor1,{ips[i]: 1 for i in range(workers)}, {0:"c_custkey", 1:"o_custkey"})
joined = task_graph.new_non_blocking_node({0:temp, 1: lineitem},None, join_executor2, {ips[i]: 1 for i in range(workers)}, {0: "o_orderkey", 1:"l_orderkey"})

def partition_key1(data, source_channel, target_channel):

    if source_channel == target_channel:
        return data
    else:
        return None

agg_executor = AggExecutor(fill_value = 0)
intermediate = task_graph.new_non_blocking_node({0:joined}, None, agg_executor, {ips[i]: 1 for i in range(workers)}, {0:partition_key1})
agg_executor1 = AggExecutor(final_func=final_func)
agged = task_graph.new_blocking_node({0:intermediate}, None, agg_executor1, {'localhost':1}, {0:None})

task_graph.create()

start = time.time()
task_graph.run_with_fault_tolerance()
print("total time ", time.time() - start)

print(ray.get(agged.to_pandas.remote()))
