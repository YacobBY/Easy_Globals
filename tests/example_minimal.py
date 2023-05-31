from EasyGlobals import EasyGlobals
g = EasyGlobals.Globals()
g.test1 = 4

print(g.test1)

g.test2 = 'hello world'
g.test3 = {'dictkey1': g.test1, 'dictkey2': g.test2} #  Dict

print(g.test1)
print(g.test2)
print(g.test3)



