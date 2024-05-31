"""Bridgy user-facing views: front page, user pages, and delete POSTs.
"""
from pathlib import Path
import string
import sys

from flask import Flask
import flask_gae_static
import humanize
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil import appengine_info

import granary
import appengine_config  # *after* import granary to override set_user_agent()
import models
import util


# Flask app
app = Flask(__name__, static_folder=None)
app.template_folder = './templates'
app.json.compact = False
app.config.from_pyfile(Path(__file__).parent / 'config.py')
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
app.before_request(flask_util.canonicalize_domain(
  util.OTHER_DOMAINS, util.PRIMARY_DOMAIN))
if (appengine_info.LOCAL_SERVER
    # ugly hack to infer if we're running unit tests
    and 'unittest' not in sys.modules):
  flask_gae_static.init_app(app)

app.wsgi_app = flask_util.ndb_context_middleware(app.wsgi_app, client=ndb_client)

app.jinja_env.globals.update({
  'naturaltime': util.naturaltime,
  'get_logins': util.get_logins,
  'sources': models.sources,
  'string': string,
  'util': util,
  'EPOCH': util.EPOCH,
})


@app.route('/_ah/<any(start, stop, warmup):_>')
def noop(_):
  return 'OK'
