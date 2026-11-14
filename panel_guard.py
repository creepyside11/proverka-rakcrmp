"""Launch the installed panel with Telegram-only authentication."""
import sys
import types
import os

# The installer records the admin under the singular name. Ignore any
# machine token so external callers cannot use the read-only shortcut.
os.environ['TG_ADMIN_IDS'] = os.environ.get('TG_ADMIN_ID', '')
os.environ.pop('PANEL_TOKEN', None)

# The upstream key is shared across installations, so it cannot authorize
# management of a particular VPS. Never import or accept it here.
license_module = types.ModuleType('web.license')
license_module.verify = lambda token: None
sys.modules['web.license'] = license_module

from web import server

class TelegramOnlyHandler(server.Handler):
    def do_POST(self):
        path = self.path.partition('?')[0]
        if path in ('/api/key', '/api/login'):
            self._json({'error': 'Откройте панель через Telegram'}, 403)
            return
        return super().do_POST()

server.Handler = TelegramOnlyHandler

if __name__ == '__main__':
    sys.exit(server.main())
