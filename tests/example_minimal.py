from src.EasyGlobals import EasyGlobals
globals = EasyGlobals.Globals()
print(globals.test1)

globals.test1 = 4
globals.test2 = 'hello world'
globals.test3 = {'dictkey1': globals.test1, 'dictkey2': globals.test2} #  Dict

print(globals.test1)
print(globals.test2)
print(globals.test3)


# ---------------------

#  Example of a full class:
class TestClass:
    def __init__(self):
        self.a = 4
        self.b = 'test'

#  Getting and setting values from nested objects directly in the Globals can be problematic as the values are pickled while they are uploaded.
# It's best to retrieve the object, modify it locally, and then store the entire object again

# This works
globals.testclass = TestClass()
print(globals.testclass.a)

#  Nested variable setting doesn't work (yet?), can probably be implemented if there's demand.
globals.testclass.a = 5
print(globals.testclass.a)

# Do this instead:
holder = globals.testclass
holder.a = 99
globals.testclass = holder
print(globals.testclass.a)

