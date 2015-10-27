# coding=utf-8
"""Misc utility constants and classes.
"""

import collections
import Cookie
import datetime
import json
import re
import urllib
import urlparse

import webapp2

from appengine_config import HTTP_TIMEOUT, DEBUG
from granary import source as gr_source
from oauth_dropins.webutil import handlers as webutil_handlers
from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import *

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset(('kylewm.com', 'snarfed.org'))

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'

# rate limiting errors. twitter returns 429, instagram 503, google+ 403.
# TODO: facebook. it returns 200 and reports the error in the response.
# https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
HTTP_RATE_LIMIT_CODES = frozenset(('403', '429', '503'))

USER_AGENT_HEADER = {'User-Agent': 'Bridgy (https://brid.gy/about)'}

# alias allows unit tests to mock the function
now_fn = datetime.datetime.now

# Domains that don't support webmentions. Mainly just the silos.
# Subdomains are automatically blacklisted too.
#
# We also check this when a user sign up and we extract the web site links from
# their profile. We automatically omit links to these domains.
with open('domain_blacklist.txt') as f:
  BLACKLIST = {l.strip() for l in f
               if l.strip() and not l.strip().startswith('#')}

# Individual URLs that we shouldn't fetch. Started because of
# https://github.com/snarfed/bridgy/issues/525 . Hopefully temporary and can be
# removed once https://github.com/idno/Known/issues/1088 is fixed!
URL_BLACKLIST = frozenset((
  'http://www.evdemon.org/2015/learning-more-about-quill',
))

# Average HTML page size as of 2015-10-15 is 56K, so this is very generous and
# conservative.
# http://www.sitepoint.com/average-page-weight-increases-15-2014/
# http://httparchive.org/interesting.php#bytesperpage
MAX_HTTP_RESPONSE_SIZE = 5000000

# Returned as the HTTP status code when we refuse to make or finish a request.
HTTP_REQUEST_REFUSED_STATUS_CODE = 599

# Unpacked representation of logged in account in the logins cookie.
Login = collections.namedtuple('Login', ('site', 'name', 'path'))

canonicalize_domain = webutil_handlers.redirect(
  ('brid-gy.appspot.com', 'www.brid.gy'), 'brid.gy')


def add_poll_task(source, now=False, **kwargs):
  """Adds a poll task for the given source entity.

  Pass now=True to insert a poll-now task.

  Tasks inserted from a backend (e.g. twitter_streaming) are sent to that
  backend by default, which doesn't work in the dev_appserver. Setting the
  target version to 'default' in queue.yaml doesn't work either, but setting it
  here does.

  Note the constant. The string 'default' works in dev_appserver, but routes to
  default.brid-gy.appspot.com in prod instead of brid.gy, which breaks SSL
  because appspot.com doesn't have a third-level wildcard cert.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  queue = 'poll-now' if now else 'poll'
  task = taskqueue.add(queue_name=queue,
                       params={'source_key': source.key.urlsafe(),
                               'last_polled': last_polled_str},
                       **kwargs)
  logging.info('Added %s task %s with args %s', queue, task.name, kwargs)


def add_propagate_task(entity, **kwargs):
  """Adds a propagate task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate',
                       params={'response_key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate task: %s', task.name)


def add_propagate_blogpost_task(entity, **kwargs):
  """Adds a propagate-blogpost task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate-blogpost',
                       params={'key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate-blogpost task: %s', task.name)


def webmention_endpoint_cache_key(url):
  """Returns memcache key for a cached webmention endpoint for a given URL.

  Example: 'W https snarfed.org'
  """
  domain = util.domain_from_link(url)
  scheme = urlparse.urlparse(url).scheme
  return ' '.join(('W', scheme, domain))


def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.warning('Error sending notification email', exc_info=True)


def requests_get(url, **kwargs):
  """Wraps requests.get and injects our timeout and user agent.

  If a server tells us a response will be too big (based on Content-Length), we
  hijack the response and return 599 and an error response body instead. We pass
  stream=True to requests.get so that it doesn't fetch the response body until
  we access response.content (or .text).

  http://docs.python-requests.org/en/latest/user/advanced/#body-content-workflow
  """
  if url in URL_BLACKLIST:
    resp = requests.Response()
    resp.status_code = HTTP_REQUEST_REFUSED_STATUS_CODE
    resp._text = resp._content = 'Sorry, Bridgy has blacklisted this URL.'
    return resp

  kwargs.setdefault('headers', {}).update(USER_AGENT_HEADER)
  kwargs.setdefault('timeout', HTTP_TIMEOUT)
  resp = requests.get(url, stream=True, **kwargs)

  length = resp.headers.get('Content-Length', 0)
  if util.is_int(length) and int(length) > MAX_HTTP_RESPONSE_SIZE:
    resp.status_code = HTTP_REQUEST_REFUSED_STATUS_CODE
    resp._text = resp._content = ('Content-Length %s is larger than our limit %s.' %
                                  (length, MAX_HTTP_RESPONSE_SIZE))

  return resp


def follow_redirects(url, cache=True):
  """Wraps granary.source.follow_redirects and injects our settings.

  ...specifically memcache and USER_AGENT_HEADER.
  """
  return gr_source.follow_redirects(url, cache=memcache if cache else None,
                                    headers=USER_AGENT_HEADER)


def get_webmention_target(url, resolve=True):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Note that this ignores failed HTTP requests, ie the boolean in the returned
  tuple will be true! TODO: check callers and reconsider this.

  Args:
    url: string
    resolve: whether to follow redirects

  Returns: (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it's a bad
    URL, not text/html, or in the blacklist.
  """
  url = util.clean_url(url)
  try:
    domain = domain_from_link(url).lower()
  except BaseException:
    logging.warning('Dropping bad URL %s.', url)
    return url, None, False

  send = True
  if resolve:
    # this follows *all* redirects, until the end
    resolved = follow_redirects(url, cache=memcache)
    send = resolved.headers.get('content-type', '').startswith('text/html')
    url, domain, _ = get_webmention_target(resolved.url, resolve=False)

  send = send and domain and not in_webmention_blacklist(domain)
  return replace_test_domains_with_localhost(url), domain, send


def in_webmention_blacklist(domain):
  """Returns True if the domain or its root domain is in BLACKLIST."""
  domain = domain.lower()
  return (domain in BLACKLIST or
          # strip subdomain and check again
          (domain and '.'.join(domain.split('.')[-2:]) in BLACKLIST))


def prune_activity(activity):
  """Prunes an activity down to just id, url, content, to, and object, in place.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since granary.Source.is_public() defaults to saying an activity is
  public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns: pruned activity dict
  """
  keep = ['id', 'url', 'content', 'fb_id', 'fb_object_id', 'fb_object_type']
  if not gr_source.Source.is_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


def prune_response(response):
  """Returns a response object dict with a few fields removed.

  Args:
    response: ActivityStreams response object

  Returns: pruned response object
  """
  obj = response.get('object')
  if obj:
    response['object'] = prune_response(obj)

  drop = ['activity', 'mentions', 'originals', 'replies', 'tags']
  return trim_nulls({k: v for k, v in response.items() if k not in drop})


def replace_test_domains_with_localhost(url):
  """Replace domains in LOCALHOST_TEST_DOMAINS with localhost for local
  testing when in DEBUG mode.

  Args:
    url: a string

  Returns: a string with certain well-known domains replaced by localhost
  """
  if url and DEBUG:
    for test_domain in LOCALHOST_TEST_DOMAINS:
      url = re.sub('https?://' + test_domain,
                   'http://localhost', url)
  return url


class Handler(webapp2.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines.
    """
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state, **kwargs):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins CallbackHandler finish() and get()
    methods, respectively.

    Args:
      source_cls: source class, e.g. Instagram
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. a JSON serialized dict
        with operation, feature, and an optional callback URL. For deletes,
        it will also include the source key
      kwargs: passed through to the source_cls constructor

    Returns:
      source entity if it was created or updated, otherwise None
    """
    state_obj = self.decode_state_parameter(state)
    operation = state_obj.get('operation', 'add')
    feature = state_obj.get('feature')
    callback = state_obj.get('callback')
    user_url = state_obj.get('user_url')

    logging.debug(
      'maybe_add_or_delete_source with operation=%s, feature=%s, callback=%s',
      operation, feature, callback)

    if operation == 'add':  # this is an add/update
      if not auth_entity:
        if not self.messages:
          self.messages.add("OK, you're not signed up. Hope you reconsider!")
        if callback:
          callback = util.add_query_params(callback, {'result': 'declined'})
          logging.debug(
            'user declined adding source, redirect to external callback %s',
            callback)
          # call super.redirect so the callback url is unmodified
          super(Handler, self).redirect(callback.encode('utf-8'))
        else:
          self.redirect('/')
        return

      CachedPage.invalidate('/users')
      logging.info('%s.create_new with %s', source_cls.__class__.__name__,
                   (auth_entity.key, state, kwargs))
      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=feature.split(',') if feature else [],
                                     user_url=user_url, **kwargs)

      if source:
        # add to login cookie
        logins = self.get_logins()
        logins.append(Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                            name=source.label_name()))
        self.set_logins(logins)

      if callback:
        callback = util.add_query_params(callback, {
          'result': 'success',
          'user': source.bridgy_url(self),
          'key': source.key.urlsafe(),
        } if source else {'result': 'failure'})
        logging.debug(
          'finished adding source, redirect to external callback %s', callback)
        # call super.redirect so the callback url is unmodified
        super(Handler, self).redirect(callback.encode('utf-8'))
      else:
        self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add('If you want to disable, please approve the %s prompt.' %
                          source_cls.GR_CLASS.NAME)
        self.redirect_home_or_user_page(state)

  def construct_state_param_for_add(self, state=None, **kwargs):
    """Construct the state parameter if one isn't explicitly passed in.
    """
    state_obj = self.decode_state_parameter(state)
    if not state_obj:
      state_obj = {field: self.request.get(field) for field in
                   ('callback', 'feature', 'id', 'user_url')}
      state_obj['operation'] = 'add'

    if kwargs:
      state_obj.update(kwargs)

    return self.encode_state_parameter(state_obj)

  def encode_state_parameter(self, obj):
    """The state parameter is passed to various source authorization
    endpoints and returned in a callback. This encodes a JSON object
    so that it can be safely included as a query string parameter.

    The following keys are common:
      - operation: 'add' or 'delete'
      - feature: 'listen', 'publish', or 'webmention'
      - callback: an optional external callback, that we will redirect to at
                  the end of the authorization handshake
      - source: the source key, only applicable to deletes

    Args:
      obj: a JSON-serializable dict

    Returns: a string
    """
    # pass in custom separators to cut down on whitespace, and sort keys for
    # unit test consistency
    return json.dumps(trim_nulls(obj), separators=(',', ':'), sort_keys=True)

  def decode_state_parameter(self, state):
    """The state parameter is passed to various source authorization
    endpoints and returned in a callback. This decodes a
    JSON-serialized string and returns a dict.

    See encode_state_parameter for a list of common state parameter
    keys.

    Args:
      state: a string (JSON-serialized dict)

    Returns: a dict containing operation, feature, and possibly other fields
    """
    logging.debug('decoding state "%s"' % state)
    obj = json.loads(state) if state else {}
    if not isinstance(obj, dict):
      logging.error('got a non-dict state parameter %s', state)
      return None
    return obj

  def get_logins(self):
    """Extracts the current user page paths from the logins cookie.

    Returns: list of Login objects
    """
    cookie = self.request.headers.get('Cookie', '')
    if cookie:
      logging.info('Cookie: %s', cookie)

    logins_str = Cookie.SimpleCookie(cookie).get('logins')
    if not logins_str or not logins_str.value:
      return []

    logins = []
    for val in set(urllib.unquote_plus(logins_str.value).decode('utf-8').split('|')):
      path, name = val.split('?')
      site, _ = path.strip('/').split('/')
      logins.append(Login(path=path, site=site, name=name))

    return logins

  def set_logins(self, logins):
    """Sets a logins cookie.

    Args:
      logins: sequence of Login objects
    """
    # cookie docs: http://curl.haxx.se/rfc/cookie_spec.html
    cookie = Cookie.SimpleCookie()
    cookie['logins'] = '|'.join(sorted(set(
      '%s?%s' % (login.path, urllib.quote_plus(login.name.encode('utf-8')))
      for login in logins)))
    cookie['logins']['path'] = '/'
    cookie['logins']['expires'] = now_fn() + datetime.timedelta(days=365 * 2)

    header = cookie['logins'].OutputString()
    logging.info('Set-Cookie: %s', header)
    self.response.headers['Set-Cookie'] = header

  def redirect_home_or_user_page(self, state):
    redirect_to = '/'
    split = state.split('-', 1)
    if len(split) >= 2:
      source = ndb.Key(urlsafe=split[1]).get()
      if source:
        redirect_to = source.bridgy_url(self)
    self.redirect(redirect_to)

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL
    - set 'website_links' attr to list of pretty HTML links to domain_urls

    Args:
      source: Source entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    source.website_links = [
      util.pretty_link(url, attrs={'rel': 'me', 'class': 'u-url'})
      for url in source.domain_urls]
    return source


def oauth_starter(oauth_start_handler, **kwargs):
  """Returns an oauth-dropins start handler that injects the state param.

  Args:
    oauth_start_handler: oauth-dropins StartHandler to use,
      e.g. oauth_dropins.twitter.StartHandler.
    kwargs: passed to construct_state_param_for_add()
  """
  class StartHandler(oauth_start_handler, Handler):
    def redirect_url(self, state=None):
      return super(StartHandler, self).redirect_url(
        self.construct_state_param_for_add(state, **kwargs))

  return StartHandler


class CachedPage(StringIdModel):
  """Cached HTML for pages that changes rarely. Key id is path.

  Stored in the datastore since datastore entities in memcache (mostly
  Responses) are requested way more often, so it would get evicted
  out of memcache easily.

  Keys, useful for deleting from memcache:
  /: aglzfmJyaWQtZ3lyEQsSCkNhY2hlZFBhZ2UiAS8M
  /users: aglzfmJyaWQtZ3lyFgsSCkNhY2hlZFBhZ2UiBi91c2Vycww
  """
  html = ndb.TextProperty()
  expires = ndb.DateTimeProperty()

  @classmethod
  def load(cls, path):
    cached = CachedPage.get_by_id(path)
    if cached:
      if cached.expires and now_fn() > cached.expires:
        logging.info('Deleting expired cached page for %s', path)
        cached.key.delete()
        return None
      else:
        logging.info('Found cached page for %s', path)
    return cached

  @classmethod
  def store(cls, path, html, expires=None):
    """path and html are strings, expires is a datetime.timedelta."""
    logging.info('Storing new page in cache for %s', path)
    if expires is not None:
      logging.info('  (expires in %s)', expires)
      expires = now_fn() + expires
    CachedPage(id=path, html=html, expires=expires).put()

  @classmethod
  def invalidate(cls, path):
    logging.info('Deleting cached page for %s', path)
    CachedPage(id=path).key.delete()
