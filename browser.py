"""Browser extension request handlers.
"""
import copy
from datetime import timedelta
import logging
from operator import itemgetter

from google.cloud import ndb
from granary import instagram as gr_instagram
from granary import microformats2
from granary import source as gr_source
from oauth_dropins import indieauth
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
from models import Activity, Domain, Source, MAX_AUTHOR_URLS
import util

JSON_CONTENT_TYPE = 'application/json'


def merge_by_id(existing, updates):
  """Merges two lists of AS1 objects by id.

  Overwrites the objects in the existing list with objects in the updates list
  with the same id. Requires all objects to have ids.

  Args:
    existing: sequence of AS1 dicts
    updates: sequence of AS1 dicts

  Returns: merged list of AS1 dicts
  """
  objs = {o['id']: o for o in existing}
  objs.update({o['id']: o for o in updates})
  return sorted(objs.values(), key=itemgetter('id'))


class BrowserSource(Source):
  """A source whose data is provided by the browser extension.

  Current subclasses are Instagram and Facebook.
  """
  CAN_LISTEN = True
  CAN_PUBLISH = False
  AUTO_POLL = False
  SLOW_POLL = FAST_POLL = timedelta(0)

  # set by subclasses
  GR_CLASS = None
  OAUTH_START_HANDLER = None
  gr_source = None

  domain_tokens = ndb.StringProperty(repeated=True)

  @classmethod
  def key_id_from_actor(cls, actor):
    """Returns the key id for this entity from a given AS1 actor.

    To be implemented by subclasses.

    Args:
      actor: dict AS1 actor

    Returns: str, key id to use for the corresponding datastore entity
    """
    raise NotImplementedError()

  @classmethod
  def new(cls, handler, auth_entity=None, actor=None, **kwargs):
    """Creates and returns an entity based on an AS1 actor.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: unused
      actor: dict AS1 actor
    """
    assert not auth_entity
    assert actor

    if not kwargs.get('features'):
      kwargs['features'] = ['listen']

    src = cls(id=cls.key_id_from_actor(actor),
              name=actor.get('displayName'),
              picture=actor.get('image', {}).get('url'),
              **kwargs)
    src.domain_urls, src.domains = src._urls_and_domains(None, None, actor=actor)
    return src

  @classmethod
  def button_html(cls, feature, **kwargs):
    return cls.OAUTH_START_HANDLER.button_html(
      '/about#browser-extension',
      form_method='get',
      image_prefix='/oauth_dropins/static/')

  def get_activities_response(self, *args, **kwargs):
    """Uses Activity entities stored in the datastore."""
    activities = []

    activity_id = kwargs.get('activity_id')
    if activity_id:
      activity = Activity.get_by_id(self.gr_source.tag_uri(activity_id))
      if activity:
        activities = [activity]
    else:
      activities = Activity.query(Activity.source == self.key)\
                           .order(-Activity.updated).fetch(50)

    return self.gr_source.make_activities_base_response(
      [json_loads(a.activity_json) for a in activities])

  def get_comment(self, comment_id,  activity=None, **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for reply in activity.get('object', {}).get('replies', {}).get('items', []):
        parsed = util.parse_tag_uri(reply.get('id', ''))
        if parsed and parsed[1] == comment_id:
          return reply

  def get_like(self, activity_user_id, activity_id, like_user_id, activity=None,
               **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for tag in activity.get('object', {}).get('tags', []):
        if tag.get('verb') == 'like':
          parsed = util.parse_tag_uri(tag.get('author', {}).get('id', ''))
          if parsed and parsed[1] == like_user_id:
            return tag


class BrowserHandler(util.Handler):
  """Base class for requests from the browser extension."""
  def output(self, obj):
    self.response.headers['Content-Type'] = JSON_CONTENT_TYPE
    self.response.write(json_dumps(obj, indent=2))

  def source_class(self):
    return models.sources.get(self.request.path.strip('/').split('/')[0])

  def gr_source(self):
    return self.source_class().gr_source

  def check_token_for_actor(self, actor):
    """Checks that the given actor is public and matches the request's token.

    Raises: :class:`HTTPException` with HTTP 400
    """
    if not gr_source.Source.is_public(actor):
      self.abort(400, f'Your {self.gr_source().NAME} account is private. Bridgy only supports public accounts.')

    token = util.get_required_param(self, 'token')
    src_cls = self.source_class()

    domains = set(util.domain_from_link(u) for u in microformats2.object_urls(actor))

    # TEMPORARY since we've lost the profile's domains in Activity contents for
    # Instagram for some reason. :/
    url = actor.get('url')
    if url:
      logging.info(f'Looking up source by URL {url}')
      source = src_cls.query(src_cls.url == url).get()
      if source:
        domains.update(source.domains)

    domains.discard(src_cls.GR_CLASS.DOMAIN)

    logging.info(f'Checking token against domains {domains}')
    for domain in ndb.get_multi(ndb.Key(Domain, d) for d in domains):
      if domain and token in domain.tokens:
        return

    self.abort(403, f'Token {token} is not authorized for any of: {domains}')

  def auth(self, actor=None, check_token=True):
    """Loads the source and (optionally) token and checks that they're valid.

    Expects token in the `token` query param, source in `key` or `username` with
    fallback to `actor` for older browser extension versions that didn't send
    them.

    Args:
      actor: dict, optional, AS1 actor
      check_token: boolean, optional, whether to load and check the token too

    Raises: :class:`HTTPException` with HTTP 400 if the token or source are
      missing or invalid

    Returns: BrowserSource or None
    """
    # Load source
    key = self.request.get('key')
    username = self.request.get('username')
    if not username and actor:
      username = actor.get('username')

    source = None
    if key:
      source = util.load_source(self, param='key')
    elif username:
      source = self.source_class().get_by_id(username)
    else:
      self.abort(400, 'No key or username query param and no scraped actor found')

    if not source:
      self.abort(404, f'No account found for {self.gr_source().NAME} user {key or username}')

    # Load and check token
    if not check_token:
      return source

    token = util.get_required_param(self, 'token')
    for domain in Domain.query(Domain.tokens == token):
      if domain.key.id() in source.domains:
        return source

    self.abort(403, f'Token {token} is not authorized for any of: {source.domains}')


class HomepageHandler(BrowserHandler):
  """Parses a silo home page and returns the logged in user's username.

  Request body is https://www.instagram.com/ HTML for a logged in user.
  """
  def post(self):
    gr_src = self.gr_source()
    _, actor = gr_src.scraped_to_activities(self.request.text)
    logging.info(f'Got actor: {actor}')

    if actor:
      # TODO?
      username = actor.get('username')
      if username:
        logging.info(f"Returning {username}")
        return self.output(username)

    self.abort(400, f"Couldn't determine logged in {gr_src.NAME} user or username")

class ProfileHandler(BrowserHandler):
  """Parses a silo profile page and returns the posts' URLs.

  Request body is HTML from an IG profile, eg https://www.instagram.com/name/ ,
  for a logged in user.

  Response body is the JSON list of translated ActivityStreams activities.
  """
  def post(self):
    gr_src = self.gr_source()

    activities, actor = gr_src.scraped_to_activities(self.request.text)
    if not actor:
      actor = gr_src.scraped_to_actor(self.request.text)

    self.check_token_for_actor(actor)

    # create/update the Bridgy account
    source = self.source_class().create_new(self, actor=actor)

    ids = ' '.join(a['id'] for a in activities)
    logging.info(f"Returning activities for {source}: {ids}")
    self.output(activities)


class PostHandler(BrowserHandler):
  """Parses a silo post's HTML and creates or updates an Activity.

  Request body is HTML from a silo post, eg https://www.instagram.com/p/ABC123/

  Response body is the translated ActivityStreams activity JSON.
  """
  def post(self):
    gr_src = self.gr_source()
    new_activity, actor = gr_src.scraped_to_activity(self.request.text)
    if not new_activity:
      self.abort(400, f'No {gr_src.NAME} post found in HTML')

    source = self.auth(actor=actor)

    @ndb.transactional()
    def update_activity():
      id = new_activity.get('id')
      if not id:
        self.abort(400, 'Scraped post missing id')
      activity = Activity.get_by_id(id)

      if activity:
        # we already have this activity! merge in any new comments.
        merged_activity = copy.deepcopy(new_activity)
        existing_activity = json_loads(activity.activity_json)
        # TODO: extract out merging replies
        replies = merged_activity.setdefault('object', {}).setdefault('replies', {})
        gr_source.merge_by_id(replies, 'items',
          existing_activity.get('object', {}).get('replies', {}).get('items', []))
        replies['totalItems'] = len(replies.get('items', []))
        # TODO: merge tags too
        activity.activity_json = json_dumps(merged_activity)
      else:
        activity = Activity(id=id, source=source.key,
                            activity_json=json_dumps(new_activity))

      # store and return the activity
      activity.put()
      logging.info(f"Stored activity {id}")
      self.output(new_activity)

    update_activity()


class LikesHandler(BrowserHandler):
  """Parses likes from silo HTML or JSON and adds them to an existing Activity.

  Requires the request parameter `id` with the silo post's id (not shortcode!).

  Response body is the translated ActivityStreams JSON for the likes.
  """
  def post(self):
    gr_src = self.gr_source()
    id = util.get_required_param(self, 'id')

    # validate request
    parsed_id = util.parse_tag_uri(id)
    if not parsed_id:
      self.abort(400, f'Expected id to be tag URI; got {id}')

    activity = Activity.get_by_id(id)
    if not activity:
      self.abort(404, f'No {gr_src.NAME} post found for id {id}')

    activity_data = json_loads(activity.activity_json)
    obj = activity_data['object']
    actor = obj.get('author') or activity_data.get('actor')
    self.check_token_for_actor(actor)

    # convert new likes to AS, merge into existing activity
    new_likes = gr_src.merge_scraped_reactions(self.request.text, activity_data)
    activity.activity_json = json_dumps(activity_data)
    activity.put()

    like_ids = ' '.join(l['id'] for l in new_likes)
    logging.info(f"Stored likes for activity {id}: {like_ids}")
    self.output(new_likes)


class PollHandler(BrowserHandler):
  """Triggers a poll for a browser-based account."""
  def post(self):
    source = self.auth(check_token=False)
    util.add_poll_task(source)
    self.output('OK')


class TokenDomainsHandler(BrowserHandler):
  """Returns the domains that a token is registered for."""
  def post(self):
    token = util.get_required_param(self, 'token')

    domains = [d.key.id() for d in Domain.query(Domain.tokens == token)]
    if not domains:
      self.abort(404, f'No registered domains for token {token}')

    self.output(domains)


def routes(source_cls):
  """Returns browser extension webapp2 routes for a given source class.

  ...specifically, with the source's short name as the routes' URL prefix.
  """
  return [
    (f'/{source_cls.SHORT_NAME}/browser/homepage', HomepageHandler),
    (f'/{source_cls.SHORT_NAME}/browser/profile', ProfileHandler),
    (f'/{source_cls.SHORT_NAME}/browser/post', PostHandler),
    (f'/{source_cls.SHORT_NAME}/browser/likes', LikesHandler),
    (f'/{source_cls.SHORT_NAME}/browser/poll', PollHandler),
    (f'/{source_cls.SHORT_NAME}/browser/token-domains', TokenDomainsHandler),
  ]
