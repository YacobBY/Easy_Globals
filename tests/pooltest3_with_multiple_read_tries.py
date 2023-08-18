# from EasyGlobals import EasyGlobals
# import attribute_test as EasyGlobals
import time
from src.EasyGlobals import EasyGlobals
import multiprocessing
from loguru import logger
from tests.class_testing.Project_Classes import LaserAttributes

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
            g.laser1 = baselaser
            g.laser1 = currenttime
        elif process_number == 2:
            g.laser2 = currenttime
        elif process_number == 3:
            g.laser3 = currenttime
        elif process_number == 4:
            g.laser4 = currenttime

        if process_number == 1:
            result = g.laser1
        elif process_number == 2:
            result = g.laser2
        elif process_number == 3:
            result = g.laser3
        elif process_number == 4:
            result = g.laser4

        latency = time.time() - result
        # logger.debug(f'Process: {process_number}, Latency: {latency}')
        if latency > max_latency:
            max_latency = latency
            logger.debug(f'Process {process_number}, max latency: {max_latency}')


    logger.debug(f'Process {process_number}, max latency: {max_latency}')

baselaser = LaserAttributes()
g.laser1 = baselaser
g.laser2 = baselaser
g.laser3 = baselaser
g.laser4 = baselaser
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
