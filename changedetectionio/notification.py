import apprise
import time
from jinja2 import Environment, BaseLoader
from apprise import NotifyFormat
import json
from loguru import logger

valid_tokens = {
    'base_url': '',
    'current_snapshot': '',
    'diff': '',
    'diff_added': '',
    'diff_full': '',
    'diff_patch': '',
    'diff_removed': '',
    'diff_url': '',
    'preview_url': '',
    'triggered_text': '',
    'watch_tag': '',
    'watch_title': '',
    'watch_url': '',
    'watch_uuid': '',
}

default_notification_format_for_watch = 'System default'
default_notification_format = 'Text'
default_notification_body = '{{watch_url}} had a change.\n---\n{{diff}}\n---\n'
default_notification_title = 'ChangeDetection.io Notification - {{watch_url}}'

valid_notification_formats = {
    'Text': NotifyFormat.TEXT,
    'Markdown': NotifyFormat.MARKDOWN,
    'HTML': NotifyFormat.HTML,
    # Used only for editing a watch (not for global)
    default_notification_format_for_watch: default_notification_format_for_watch
}

# include the decorator
from apprise.decorators import notify

@notify(on="delete")
@notify(on="deletes")
@notify(on="get")
@notify(on="gets")
@notify(on="post")
@notify(on="posts")
@notify(on="put")
@notify(on="puts")
def apprise_custom_api_call_wrapper(body, title, notify_type, *args, **kwargs):
    import requests
    from apprise.utils import parse_url as apprise_parse_url
    from apprise.URLBase import URLBase

    url = kwargs['meta'].get('url')

    if url.startswith('post'):
        r = requests.post
    elif url.startswith('get'):
        r = requests.get
    elif url.startswith('put'):
        r = requests.put
    elif url.startswith('delete'):
        r = requests.delete

    url = url.replace('post://', 'http://')
    url = url.replace('posts://', 'https://')
    url = url.replace('put://', 'http://')
    url = url.replace('puts://', 'https://')
    url = url.replace('get://', 'http://')
    url = url.replace('gets://', 'https://')
    url = url.replace('put://', 'http://')
    url = url.replace('puts://', 'https://')
    url = url.replace('delete://', 'http://')
    url = url.replace('deletes://', 'https://')

    headers = {}
    params = {}
    auth = None

    # Convert /foobar?+some-header=hello to proper header dictionary
    results = apprise_parse_url(url)
    if results:
        # Add our headers that the user can potentially over-ride if they wish
        # to to our returned result set and tidy entries by unquoting them
        headers = {URLBase.unquote(x): URLBase.unquote(y)
                   for x, y in results['qsd+'].items()}

        # https://github.com/caronc/apprise/wiki/Notify_Custom_JSON#get-parameter-manipulation
        # In Apprise, it relies on prefixing each request arg with "-", because it uses say &method=update as a flag for apprise
        # but here we are making straight requests, so we need todo convert this against apprise's logic
        for k, v in results['qsd'].items():
            if not k.strip('+-') in results['qsd+'].keys():
                params[URLBase.unquote(k)] = URLBase.unquote(v)

        # Determine Authentication
        auth = ''
        if results.get('user') and results.get('password'):
            auth = (URLBase.unquote(results.get('user')), URLBase.unquote(results.get('user')))
        elif results.get('user'):
            auth = (URLBase.unquote(results.get('user')))

    # Try to auto-guess if it's JSON
    try:
        json.loads(body)
        headers['Content-Type'] = 'application/json; charset=utf-8'
    except ValueError as e:
        pass

    r(results.get('url'),
      auth=auth,
      data=body,
      headers=headers,
      params=params
      )


def process_notification(n_object, datastore):

    # Insert variables into the notification content
    notification_parameters = create_notification_parameters(n_object, datastore)

    # Get the notification body from datastore
    jinja2_env = Environment(loader=BaseLoader)
    n_body = jinja2_env.from_string(n_object.get('notification_body', '')).render(**notification_parameters)
    n_title = jinja2_env.from_string(n_object.get('notification_title', '')).render(**notification_parameters)
    n_format = valid_notification_formats.get(
        n_object.get('notification_format', default_notification_format),
        valid_notification_formats[default_notification_format],
    )

    # If we arrived with 'System default' then look it up
    if n_format == default_notification_format_for_watch and datastore.data['settings']['application'].get('notification_format') != default_notification_format_for_watch:
        # Initially text or whatever
        n_format = datastore.data['settings']['application'].get('notification_format', valid_notification_formats[default_notification_format])

    # https://github.com/caronc/apprise/wiki/Development_LogCapture
    # Anything higher than or equal to WARNING (which covers things like Connection errors)
    # raise it as an exception

    sent_objs = []
    from .apprise_asset import asset
    apobj = apprise.Apprise(debug=True, asset=asset)

    if not n_object.get('notification_urls'):
        return None

    with apprise.LogCapture(level=apprise.logging.DEBUG) as logs:
        for url in n_object['notification_urls']:
            url = url.strip()
            logger.info(">> Process Notification: AppRise notifying {}".format(url))
            url = jinja2_env.from_string(url).render(**notification_parameters)

            # Re 323 - Limit discord length to their 2000 char limit total or it wont send.
            # Because different notifications may require different pre-processing, run each sequentially :(
            # 2000 bytes minus -
            #     200 bytes for the overhead of the _entire_ json payload, 200 bytes for {tts, wait, content} etc headers
            #     Length of URL - Incase they specify a longer custom avatar_url

            # So if no avatar_url is specified, add one so it can be correctly calculated into the total payload
            k = '?' if not '?' in url else '&'
            if not 'avatar_url' in url \
                    and not url.startswith('mail') \
                    and not url.startswith('post') \
                    and not url.startswith('get') \
                    and not url.startswith('delete') \
                    and not url.startswith('put'):
                url += k + 'avatar_url=https://raw.githubusercontent.com/dgtlmoon/changedetection.io/master/changedetectionio/static/images/avatar-256x256.png'

            if url.startswith('tgram://'):
                # Telegram only supports a limit subset of HTML, remove the '<br>' we place in.
                # re https://github.com/dgtlmoon/changedetection.io/issues/555
                # @todo re-use an existing library we have already imported to strip all non-allowed tags
                n_body = n_body.replace('<br>', '\n')
                n_body = n_body.replace('</br>', '\n')
                # real limit is 4096, but minus some for extra metadata
                payload_max_size = 3600
                body_limit = max(0, payload_max_size - len(n_title))
                n_title = n_title[0:payload_max_size]
                n_body = n_body[0:body_limit]

            elif url.startswith('discord://') or url.startswith('https://discordapp.com/api/webhooks') or url.startswith(
                    'https://discord.com/api'):
                # real limit is 2000, but minus some for extra metadata
                payload_max_size = 1700
                body_limit = max(0, payload_max_size - len(n_title))
                n_title = n_title[0:payload_max_size]
                n_body = n_body[0:body_limit]

            elif url.startswith('mailto'):
                # Apprise will default to HTML, so we need to override it
                # So that whats' generated in n_body is in line with what is going to be sent.
                # https://github.com/caronc/apprise/issues/633#issuecomment-1191449321
                if not 'format=' in url and (n_format == 'Text' or n_format == 'Markdown'):
                    prefix = '?' if not '?' in url else '&'
                    # Apprise format is lowercase text https://github.com/caronc/apprise/issues/633
                    n_format = n_format.lower()
                    url = f"{url}{prefix}format={n_format}"
                # If n_format == HTML, then apprise email should default to text/html and we should be sending HTML only

            apobj.add(url)

            sent_objs.append({'title': n_title,
                              'body': n_body,
                              'url': url,
                              'body_format': n_format})

        # Blast off the notifications tht are set in .add()
        apobj.notify(
            title=n_title,
            body=n_body,
            body_format=n_format,
            # False is not an option for AppRise, must be type None
            attach=n_object.get('screenshot', None)
        )

        # Give apprise time to register an error
        time.sleep(3)

        # Returns empty string if nothing found, multi-line string otherwise
        log_value = logs.getvalue()

        if log_value and 'WARNING' in log_value or 'ERROR' in log_value:
            raise Exception(log_value)

    # Return what was sent for better logging - after the for loop
    return sent_objs


# Notification title + body content parameters get created here.
# ( Where we prepare the tokens in the notification to be replaced with actual values )
def create_notification_parameters(n_object, datastore):
    from copy import deepcopy

    # in the case we send a test notification from the main settings, there is no UUID.
    uuid = n_object['uuid'] if 'uuid' in n_object else ''

    if uuid:
        watch_title = datastore.data['watching'][uuid].get('title', '')
        tag_list = []
        tags = datastore.get_all_tags_for_watch(uuid)
        if tags:
            for tag_uuid, tag in tags.items():
                tag_list.append(tag.get('title'))
        watch_tag = ', '.join(tag_list)
    else:
        watch_title = 'Change Detection'
        watch_tag = ''

    # Create URLs to customise the notification with
    # active_base_url - set in store.py data property
    base_url = datastore.data['settings']['application'].get('active_base_url')

    watch_url = n_object['watch_url']

    diff_url = "{}/diff/{}".format(base_url, uuid)
    preview_url = "{}/preview/{}".format(base_url, uuid)

    # Not sure deepcopy is needed here, but why not
    tokens = deepcopy(valid_tokens)

    # Valid_tokens also used as a field validator
    tokens.update(
        {
            'base_url': base_url,
            'current_snapshot': n_object.get('current_snapshot', ''),
            'diff': n_object.get('diff', ''),  # Null default in the case we use a test
            'diff_added': n_object.get('diff_added', ''),  # Null default in the case we use a test
            'diff_full': n_object.get('diff_full', ''),  # Null default in the case we use a test
            'diff_patch': n_object.get('diff_patch', ''),  # Null default in the case we use a test
            'diff_removed': n_object.get('diff_removed', ''),  # Null default in the case we use a test
            'diff_url': diff_url,
            'preview_url': preview_url,
            'triggered_text': n_object.get('triggered_text', ''),
            'watch_tag': watch_tag if watch_tag is not None else '',
            'watch_title': watch_title if watch_title is not None else '',
            'watch_url': watch_url,
            'watch_uuid': uuid,
        })

    return tokens
