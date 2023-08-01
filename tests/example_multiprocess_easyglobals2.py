import time

from src.EasyGlobals import EasyGlobals
import multiprocessing
g = EasyGlobals.Globals()
#
# for i in range(100_000):
#     g.testies = i
#     print(g.testies)
#     print('d')

while True:
    for i in range(100000000):
        g.testvar = i
        g[f's{i}'] = i + 10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000024343
        # print( g[f's{i}'])
        # print(i)
        # print(f'wrote: {i}')

    for i in range(10000000):
        # print(f'got {g.testvar}')
        rec= g[f's{i}']
        print(f'{i} = {rec}')
        # print(f'got: {i}')

# time.sleep(12)
# print(g.testvar)