# from EasyGlobals import EasyGlobals
# import attribute_test as EasyGlobals
import time

from src.EasyGlobals import EasyGlobals
import multiprocessing

g = EasyGlobals.Globals()
# g.reset_all_globals()
g.laser1 = time.time()
g.laser2 = time.time()
g.laser3 = time.time()
g.laser4 = time.time()
time.sleep(1)



def retrieve_from_globals(process_id):
    max_latency = 0
    g = EasyGlobals.Globals()

    print(process_id)
    time.sleep(1)
    for i in range(100_000):
        currenttime = time.time()

        if process_id == 1:
            g.laser1 = currenttime
        elif process_id == 2:
            g.laser2 = currenttime
        elif process_id == 3:
            g.laser3 = currenttime
        elif process_id == 4:
            g.laser4 = currenttime

        if process_id == 1:
            result = g.laser1
        elif process_id == 2:
            result = g.laser2
        elif process_id == 3:
            result = g.laser3
        elif process_id == 4:
            result = g.laser4

        latency = time.time() - result
        if latency > max_latency:
            max_latency = latency
        print(f'Process: {process_id}, Latency: {latency}')


    print(f'Process {process_id}, max latency: {max_latency}')

# def pooled_client_testing(process_id):
#     g = EasyGlobals.Globals()
#     max_latency = 0
#
#     id = f'id{process_id}'
#     print(f'Key: {id}')
#     g.memcached_globals_client.set(key=id, value=time.time())
#
#     for i in range(100_000):
#         if process_id == 1:
#             g.laser1 = time.time()
#         elif process_id == 2
#             g.laser2 = time.time()
#
#         g.memcached_globals_client.set(key=id, value=time.time())
#         result = g.memcached_globals_client.__getattribute__(key=id)
#
#         latency = time.time() - result
#         print(f'Process: {process_id}, Latency: {latency}')
#         if latency > max_latency:
#             max_latency = latency
#
#     print(f'Process {process_id}, max: {max_latency}')

g = EasyGlobals.Globals()
print('Start reading with 4 simultaneous processes')
processlist = []
for i in range(3):
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist[i].start()

# for process in processlist:
#     process.join()
print('Done reading')

class myclass:
    def __init__(self):
        self.x = 'tset'


class myclass2:
    def __init__(self):
        self.x = 2
        self.y = myclass()

tst = myclass2()

