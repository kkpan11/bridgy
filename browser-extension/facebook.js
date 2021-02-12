// TODO: update the profile on every poll, since the profile picture URL has a
// timestamp that expires.
'use strict'

import {Silo} from './common.js'


class Facebook extends Silo {
  DOMAIN = 'facebook.com'
  NAME = 'facebook'
  BASE_URL = 'https://mbasic.facebook.com'
  LOGIN_URL = `${this.BASE_URL}/login`
  COOKIE = 'xs'

  /**
   * Returns the URL path to the user's profile.
   */
  async profilePath() {
    return '/me/about'
  }

  /**
   * Returns the URL path to the user's feed of posts.
   */
  async feedPath() {
    return '/me'
  }

  /**
   * Returns an AS activity's reaction count, if available.
   */
  reactionsCount(activity) {
    return activity.object.fb_reaction_count
  }

  /**
   * Returns the URL path for a given activity's reactions.
   */
  reactionsPath(activity) {
      return `/ufi/reaction/profile/browser/?ft_ent_identifier=${activity.fb_id}`
  }
}

export {Facebook}