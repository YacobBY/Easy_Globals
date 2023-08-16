from EasyGlobals import EasyGlobals
# import attribute_test as EasyGlobals
import time

# from src.EasyGlobals import EasyGlobals
import multiprocessing
g = EasyGlobals.Globals()
g.reset_all_globals()

print(time.time())
def write_to_globals():
    g = EasyGlobals.Globals()
    for i in range(100_000):
        # g.testvar = i
        g.testvar2 = i
        g.testvar3 = i
        # print(f'wrote: {i}')

def retrieve_from_globals(process_id):
    g = EasyGlobals.Globals()
    for i in range(100_000):
        g.memcached_globals_client.set(key=f'key{i}', value=time.time())

        g.testvar = process_id
        result = g.testvar
        print(f'Process {process_id}, read: {result}')


starttime = time.time()
print('Start writing process')
write_process = multiprocessing.Process(target=write_to_globals)
write_process.start()
#

g = EasyGlobals.Globals()
print('Start reading with 3 simultaneous processes')
processlist = []
for i in range(10):
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist[i].start()

for process in processlist:
    process.join()
print('Done reading')
write_process.join()
print('Done writing')

print(f'Total time: {time.time() - starttime}')

class myclass:
    def __init__(self):
        self.x = 'tset'


class myclass2:
    def __init__(self):
        self.x = 2
        self.y = myclass()

tst = myclass2()
