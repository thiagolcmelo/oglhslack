#!/usr/bin/python

import os, sys, time, requests, json, urllib, re, textwrap, yaml, urlparse
from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from collections import namedtuple
from functools import wraps, partial
from slackclient import SlackClient

def ensure_auth(f):
    """
    makes sure the client has a valid token when a function is called
    the call is going to be made once, in case of not authenticated,
    it will try to authenticate and call the function again
    """
    def wrapper(*args, **kwargs):
        result = f(*args, **kwargs)
        if type(result) is dict and 'error' in result and \
                len(result['error']) > 0 and \
                result['error'][0]['level'] == 1 and \
                result['error'][0]['type'] == 7 and \
		        result['error'][0]['text'] == 'Invalid session ID':
            args[0]._do_auth()
            return f(*args, **kwargs)
        return result
    return wrapper

class LighthouseApi:
    """
    the basic API client, with methods for GET, POST, PUT, and DELETE
    """

    def __init__(self):
        self.url = 'https://oglh-octo.opengear.com'
        requests.packages.urllib3.disable_warnings()

        self.api_url = self.url + '/api/v1'
        self.username = os.environ.get('OGLH_API_USER')
        self.password = os.environ.get('OGLH_API_PASS')
        self.token = None
        self.token_timeout = 5 * 60
        self.pending_name_ids = {}
        self.s = requests.Session()

        with open("../og-rest-api-specification-v1.raml", 'r') as stream:
            self.raml = yaml.load(stream)

    def _headers(self):
        headers = { 'Content-type' : 'application/json' }
        if self.token:
            headers.update({ 'Authorization' : 'Token ' + self.token })
        return headers

    def _do_auth(self):
        url = self._get_api_url('/sessions')
        data = { 'username' : self.username, 'password' : self.password }
        self.token = None
        try:
            r = self.s.post(url, headers=self._headers(), \
                data=json.dumps(data), verify=False)
            r.raise_for_status()
        except Exception as e:
            print e
            return
        body = json.loads(r.text)
        self.token = body['session']
        if not self.token:
            raise RuntimeError('Auth failed')
        self.s.headers = self._headers()

    def _get_api_url(self, path):
        return self.api_url + path

    def _parse_response(self, response):
        try:
            return json.loads(response.text)
        except ValueError:
            return response.text

    def _get_url(self, path, **kwargs):
        return self._get_api_url(str.format(path, **kwargs))

    def _get_url_params(self, path, *args, **kwargs):
        for a in args:
            if type(a) is dict:
                kwargs.update(a)
        params = urllib.urlencode({ k: v for k,v in kwargs.iteritems() \
            if not re.match('.*\{' + k + '\}', path) })
        return self._get_url(path, **kwargs), params

    @ensure_auth
    def get(self, path, *args, **kwargs):
        url, params = self._get_url_params(path, *args, **kwargs)
        r = self.s.get(url, params=params, verify=False)
        return self._parse_response(r)

    @ensure_auth
    def post(self, path, data={}, **kwargs):
        if 'data' in kwargs and data == {}:
            data = kwargs['data']
            del kwargs['data']
        url = self._get_url(path, **kwargs)
        r = self.s.post(url, data=json.dumps(data), verify=False)
        return self._parse_response(r)

    @ensure_auth
    def put(self, path, data, **kwargs):
        if 'data' in kwargs and data == {}:
            data = kwargs['data']
            del kwargs['data']
        url = self._get_url(path, **kwargs)
        r = self.s.put(url, data=json.dumps(data), verify=False)
        return self._parse_response(r)

    @ensure_auth
    def delete(self, path, **kwargs):
        r = self.s.delete(self._get_url(path, **kwargs), verify=False)
        return self._parse_response(r)

    def get_client(self):
        return self._get_client(self.raml, '')

    def _get_client(self, node, path):
        top_children = set([key.split('/')[1] for key in node.keys() \
            if re.match('^\/', key) and len(key.split('/')) == 2])
        sub_children = set(['__'.join(key.split('/')[1:]) for key in node.keys() \
            if re.match('^\/', key) and len(key.split('/')) > 2])
        middle_children = set([s.split('__')[0] for s in sub_children])
        actions = set([key for key in node.keys() if re.match('^[^\/]', key)])

        kwargs = { 'path': path }

        for k in actions:
            if k == 'get' and re.match('.*(I|i)d\}$', path):
                kwargs['find'] = partial(self.get, path)
            elif k == 'get' and len([l for l in top_children if re.match('\{.+\}', l)]) > 0:
                kwargs['list'] = partial(self.get, path)
            elif k == 'get':
                kwargs['get'] = partial(self.get, path)
            elif k == 'put':
                kwargs['update'] = partial(self.put, path)
            elif k == 'post':
                kwargs['create'] = partial(self.post, path)
            elif k == 'delete':
                kwargs['delete'] = partial(self.delete, path)
            else:
                kwargs[k] = node[k]

        for k in top_children:
            if re.match('\{.+\}', k):
                inner_props = self._get_client(node['/' + k], path + '/' + k)
                for l in inner_props._asdict():
                    kwargs[l] = inner_props._asdict()[l]
            else:
                kwargs[k] = self._get_client(node['/' + k], path + '/' + k)

        for k in list(middle_children):
            subargs = {}
            if re.match('\{.+\}', k):
                continue
            else:
                for s in [l for l in list(sub_children) if re.match('^' + k, l)]:
                    sub = re.sub('^' + k + '__', '', s)
                    subargs[sub] = self._get_client(node['/' + k + '/' + sub], \
                        path + '/' + k + '/' + sub)
            SubClient = namedtuple('SubClient', ' '.join(subargs.keys()))
            kwargs[k] = SubClient(**subargs)

        SynClient = namedtuple('SynClient', ' '.join(kwargs.keys()))
        return SynClient(**kwargs)