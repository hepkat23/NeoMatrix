from calibre.customize import InterfaceActionBase

class FrankenBOOXSyncPlugin(InterfaceActionBase):
    name = 'NeoMatrix'
    description = 'BOOX reading data archive and Calibre library integration'
    supported_platforms = ['osx', 'windows', 'linux']
    author = 'R V'
    version = (1, 4, 13)
    minimum_calibre_version = (7, 0, 0)
    actual_plugin = 'calibre_plugins.frankenboox.ui:FrankenBOOXSyncAction'
