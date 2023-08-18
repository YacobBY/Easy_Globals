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

    laser = LaserAttributes()
    laser.last_update_time = time.time()
    process_number = process_id
    max_latency = 0
    logger.debug(process_number)

    # time.sleep(0.5)
    for i in range(100_00000):
        result = 0.0
        laser.last_update_time = time.time()

        if process_number == 1:
            g.laser1 = laser
        elif process_number == 2:
            g.laser2 = laser
        elif process_number == 3:
            g.laser3 = laser
        elif process_number == 4:
            g.laser4 = laser

        if process_number == 1:
            retreieved_laser = g.laser1
        elif process_number == 2:
            retreieved_laser = g.laser2
        elif process_number == 3:
            retreieved_laser = g.laser3
        elif process_number == 4:
            retreieved_laser = g.laser4


        latency = time.time() - retreieved_laser.last_update_time
        # logger.debug(f'Process: {process_number}, Latency: {latency}')
        if latency > max_latency:
            max_latency = latency
            logger.debug(f'Process {process_number}, max latency: {max_latency}')

    logger.debug(f'END OF PROCESS {process_id} ---')
    logger.success(f'Process {process_number}, max latency: {max_latency}')
    return

g.laser1 = time.time()
g.laser2 = time.time()
g.laser3 = time.time()
g.laser4 = time.time()

processlist = []
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(1,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(1,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(1,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(1,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(1,) ))

processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(2,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(3,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(4,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(2,) ))
processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(3,) ))

for process in processlist:
    process.start()

time.sleep(5)

for process in processlist:
    process.join()
logger.debug('Done reading')
