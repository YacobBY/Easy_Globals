# from EasyGlobals import EasyGlobals
# import attribute_test as EasyGlobals
import time
from src.EasyGlobals import EasyGlobals
import multiprocessing
from loguru import logger

# g.reset_all_globals()
g = EasyGlobals.Globals()

time.sleep(0.1)

def retrieve_from_globals(process_id):
    g = EasyGlobals.Globals()
    process_number = process_id
    max_latency = 0
    logger.debug(process_number)

    # time.sleep(0.5)
    for i in range(100_000):
        result = 0.0
        currenttime = time.time()

        if process_number == 1:
            g.memcached_globals_client.set(key='laser1', value=currenttime)
        elif process_number == 2:
            g.memcached_globals_client.set(key='laser2', value=currenttime)
        # elif process_number == 3:
        #     g.memcached_globals_client.set(key='laser3', value=currenttime)
        elif process_number == 4:
            g.memcached_globals_client.set(key='laser4', value=currenttime)
        #
        # if process_number == 1:
        #     result = g.memcached_globals_client.get(key='laser1')
        # elif process_number == 2:
        #     result = g.memcached_globals_client.get(key='laser2')
        # # elif process_number == 3:
        # #     result = g.memcached_globals_client.get(key='laser3')
        # elif process_number == 4:
        #     result = g.memcached_globals_client.get(key='laser4')

        latency = time.time() - result
        # logger.debug(f'Process: {process_number}, Latency: {latency}')
        if latency > max_latency:
            max_latency = latency

    logger.debug(f'Process {process_number}, max latency: {max_latency}')

currenttime =time.time()
g.memcached_globals_client.set(key='laser1', value=currenttime)
g.memcached_globals_client.set(key='laser2', value=currenttime)
g.memcached_globals_client.set(key='laser3', value=currenttime)
g.memcached_globals_client.set(key='laser4', value=currenttime)


processlist = []
for i in range(4):
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i+1,)))
    # processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i+1,)))
    # processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i+1,)))

for process in processlist:
    process.start()

time.sleep(5)

for process in processlist:
    process.join()
logger.debug('Done reading')
