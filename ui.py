from calibre.gui2.actions import InterfaceAction
from calibre_plugins.frankenboox.main import FrankenDialog

class FrankenBOOXSyncAction(InterfaceAction):
    name = 'NeoMatrix'
    action_spec = ('NeoMatrix', None, 'Archive and browse BOOX NeoReader reading state in Calibre', None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dialog)

    def show_dialog(self):
        FrankenDialog(self.gui).exec()
