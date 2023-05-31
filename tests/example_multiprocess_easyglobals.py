from EasyGlobals import EasyGlobals
import multiprocessing

def write_to_globals():
    g = EasyGlobals.Globals()
    for i in range(1_000):
        g.testvar = i

def retrieve_from_globals(process_id):
    g = EasyGlobals.Globals()
    for i in range(10):
        result = g.testvar
        print(f'Process {process_id}, read: {result}')


print('Start writing process')
write_process = multiprocessing.Process(target=write_to_globals)
write_process.start()

print('Start reading with 3 simultaneous processes')
processlist = []
for i in range(5):
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist[i].start()

for process in processlist:
    process.join()
print('Done reading')
write_process.join()
print('Done writing')
