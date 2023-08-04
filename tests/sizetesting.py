import time

from src.EasyGlobals import EasyGlobals
import multiprocessing
g = EasyGlobals.Globals()
g.memcached_globals_client.flush_all()
#
# for i in range(100_000):
#     g.testies = i
#     print(g.testies)
#     print('d')

for i in range(100_0000):
    g.testvar = i
    g[f's{i}'] = i + 10e200
    # print( g[f's{i}'])
    # print(i)
    # print(f'wrote: {i}')

for i in range(100):
    # print(f'got {g.testvar}')
    rec= g[f's{i}']
    print(f'{i} = {rec}')
    # print(f'got: {i}')

# time.sleep(12)
# print(g.testvar)