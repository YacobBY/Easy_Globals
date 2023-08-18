from dataclasses import dataclass
from Project_Classes import LaserAttributes, LidarScannerAttributes, PLC_to_IPC, EGM_Global_Attributes
from EasyGlobals import EasyGlobals

"""
Begin defining the global classes.
Don't make this a seperate class the init wont work when inheriting from EasyGlobals
g and Project_Globals need to have exactly the same attributes. Don't inherit, it doesn't work with the memcached override.
"""


def set_class_attributes_with_dict(input_object, input_dict):
    # Set all attribute values to None
    for attr in vars(input_object):
        setattr(input_object, attr, None)

    # Set all attributes that are in dict
    for key, value in input_dict.items():
        if hasattr(input_object, key):
            setattr(input_object, key, value)
        else:
            print(f'Error, dict key {key} not in Project_Globals class in method set_attributes_with_dict')


class Project_Globals():
    def __init__(self, variables_dict=None):

        self.plc_to_ipc = PLC_to_IPC()
        self.egm_to_plc = EGM_Global_Attributes()

        self.laser1 = LaserAttributes()
        self.laser2 = LaserAttributes()
        self.laser3 = LaserAttributes()
        self.laser4 = LaserAttributes()

        self.lidar1 = LidarScannerAttributes()
        self.lidar2 = LidarScannerAttributes()
        self.lidar3 = LidarScannerAttributes()
        self.lidar4 = LidarScannerAttributes()

        if variables_dict is not None:
            set_class_attributes_with_dict(self, variables_dict)


g = EasyGlobals.Globals()
g.plc_to_ipc = PLC_to_IPC()
g.egm_to_plc = EGM_Global_Attributes()

g.laser1 = LaserAttributes()
g.laser2 = LaserAttributes()
g.laser3 = LaserAttributes()
g.laser4 = LaserAttributes()

g.lidar1 = LidarScannerAttributes()
g.lidar2 = LidarScannerAttributes()
g.lidar3 = LidarScannerAttributes()
g.lidar4 = LidarScannerAttributes()


def set_dict_to_globals(variables_dict: dict):
    """This is arund 2x faster than individually setting variables"""
    g.memcached_globals_client.set_many(variables_dict)


def retrieve_multiple_globals_as_object(keys: list) -> Project_Globals:
    """Retrieve multiple keys at once and write them to a Project_Globals object.
    If a variable isn't in a dict it will be set to None in the object.
    This is arund 2x faster than individually getting variables"""
    globals_dict = g.memcached_globals_client.get_many(keys)
    globals_object = Project_Globals( variables_dict=globals_dict)

    return globals_object