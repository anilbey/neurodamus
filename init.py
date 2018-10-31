"""
Neurodamus is a software for handling neuronal simulation using neuron.

Copyright (c) 2018 Blue Brain Project, EPFL.
All rights reserved
"""
import sys
from neurodamus import Neurodamus
from neurodamus.core.configuration import GlobalConfig

if __name__ == "__main__":
    if "--debug" in sys.argv:
        GlobalConfig.verbosity = 3
    elif "--verbose" in sys.argv:
        GlobalConfig.verbosity = 2

    config_file = "BlueConfig"
    for arg in sys.argv:
        if arg.startswith("--configFile="):
            config_file = arg.split('=')[1]
            break
    Neurodamus(config_file).run()
