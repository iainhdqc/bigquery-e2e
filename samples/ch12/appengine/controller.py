#!/usr/bin/env python
# All rights to this package are hereby disclaimed and its contents
# released into the public domain by the authors.

import cgi
import time
import threading
import json

from google.appengine.api import users
from google.appengine.ext.webapp.util import login_required
from google.appengine.api import memcache
from google.appengine.api import app_identity
from google.appengine.api import background_thread
import webapp2
import httplib2
from oauth2client.appengine import AppAssertionCredentials
from apiclient.discovery import build
from mapreduce.mapper_pipeline import MapperPipeline
from job_runner import JobRunner
from config import PROJECT_ID
from config import GCS_BUCKET

credentials = AppAssertionCredentials(
  scope='https://www.googleapis.com/auth/bigquery')
bigquery = build('bigquery', 'v2',
                 http=credentials.authorize(httplib2.Http(memcache)))

g_state_lock = threading.RLock()
ZERO_STATE = {
  'status': 'IDLE',
  'extract_job_id': '',
  'extract_result': '',
  'load_job_id': '',
  'load_result': '',
  'mapper_link': '',
  'error': 'None',
  'refresh': '',
  }
g_state = ZERO_STATE.copy()

def pre(s):
  '''Helper function to format JSON for display.'''
  return '<pre>' + cgi.escape(str(s)) + '</pre>'

def run_bigquery_job(job_id_prefix, job_type, config):
  '''Run a bigquery job and update pipeline status.'''
  global g_state
  runner = JobRunner(PROJECT_ID,
                     job_id_prefix + '_' + job_type,
                     client=bigquery)
  runner.start_job({job_type: config})
  with g_state_lock:
    g_state[job_type + '_job_id'] = runner.job_id
  job_state = 'STARTED'
  while job_state != 'DONE':
    time.sleep(5)
    result = runner.get_job()
    job_state = result['status']['state']
    with g_state_lock:
      g_state[job_type + '_result'] = pre(json.dumps(result, indent=2))

  if 'errorResult' in result['status']:
    raise RuntimeError(json.dumps(result['status']['errorResult'], 
                       indent=2))

def wait_for_pipeline(pipeline_id):
  '''Wait for a MapReduce pipeline to complete.'''
  mapreduce_id = None
  while True:
    time.sleep(5)
    pipeline = MapperPipeline.from_id(pipeline_id)
    if not mapreduce_id and pipeline.outputs.job_id.filled:
      mapreduce_id = pipeline.outputs.job_id.value
      with g_state_lock:
        g_state['mapper_link'] = (
          '<a href="/mapreduce/detail?mapreduce_id=%s">%s</a>' % (
            mapreduce_id, mapreduce_id))
    if pipeline.has_finalized:
      break
  if pipeline.outputs.result_status.value != 'success':
    raise RuntimeError('Mapper job failed, see status link.')
  
def table_reference(table_id):
  '''Helper to construct a table reference.'''
  return {
    'projectId': PROJECT_ID,
    'datasetId': 'ch12',
    'tableId': table_id,
    }

OUTPUT_SCHEMA = {
  'fields': [
    {'name':'id', 'type':'STRING'},
    {'name':'lat', 'type':'FLOAT'},
    {'name':'lng', 'type':'FLOAT'},
    {'name':'zip', 'type':'STRING'},
    ]
  }

def run_transform():
  JOB_ID_PREFIX = 'ch12_%d' % int(time.time())
  TMP_PATH = 'tmp/mapreduce/%s' % JOB_ID_PREFIX

  # Extract from BigQuery to GCS.
  run_bigquery_job(JOB_ID_PREFIX, 'extract', {
      'sourceTable': table_reference('add_zip_input'),
      'destinationUri': 'gs://%s/%s/input-*' % (GCS_BUCKET, TMP_PATH),
      'destinationFormat': 'NEWLINE_DELIMITED_JSON',
      })

  # Run the mapper job to annotate the records.
  mapper = MapperPipeline(
    'Add Zip',
    'add_zip.apply',
    'mapreduce.input_readers.FileInputReader',
    'mapreduce.output_writers._GoogleCloudStorageOutputWriter',
    params={
      'files': ['/gs/%s/%s/input-*' % (GCS_BUCKET, TMP_PATH)],
      'format': 'lines',
      'output_writer': {
        'bucket_name': GCS_BUCKET,
        'naming_format': TMP_PATH + '/output-$num',
        }
      })
  mapper.start()
  wait_for_pipeline(mapper.pipeline_id)

  # Load from GCS into BigQuery.
  run_bigquery_job(JOB_ID_PREFIX, 'load', {
      'destinationTable': table_reference('add_zip_output'),
      'sourceUris': ['gs://%s/%s/output-*' % (GCS_BUCKET, TMP_PATH)],
      'sourceFormat': 'NEWLINE_DELIMITED_JSON',
      'schema': OUTPUT_SCHEMA,
      'writeDisposition': 'WRITE_TRUNCATE',
      })

def run_attempt():
  global g_state
  try:
    with g_state_lock:
      if g_state['status'] == 'RUNNING':
        return
      g_state = ZERO_STATE.copy()
      g_state['status'] = 'RUNNING'
    run_transform()
  except Exception, err:
    with g_state_lock:
      g_state['error'] = pre(err)
  finally:
    with g_state_lock:
      g_state['status'] = 'IDLE'

class MainHandler(webapp2.RequestHandler):
  @login_required
  def get(self):
    current = ZERO_STATE.copy()
    with g_state_lock:
      current.update(g_state)
      if current['status'] == 'RUNNING':
        current['refresh'] = '<meta http-equiv="refresh" content="6"/>'
    self.response.write(_PAGE % current)

  def post(self):
    if not users.is_current_user_admin():
      self.abort(401, 'Must be an admin to start a mapreduce.')    
    background_thread.start_new_background_thread(run_attempt, [])
    self.redirect(self.request.route.build(self.request, [], {}))

app = webapp2.WSGIApplication([
    webapp2.Route(r'/', handler=MainHandler, name='main'),
], debug=True)

_PAGE = '''<html>
<head>
<title>MapReduce controller</title>
%(refresh)s
</head>
<body>
<h1>MapReduce Controller</h1>
<div><a href="/mapreduce/status">MapReduce Status</a></div>
<div>Status: %(status)s</div>
<div>
Extract Job Id: %(extract_job_id)s
%(extract_result)s
</div>
<div>
Mapper: %(mapper_link)s
</div>
<div>
Load Job Id: %(load_job_id)s
%(load_result)s
</div>
<div>%(error)s</div>
<div>
<form action="/" method="post">
  <input type="submit" name="action" value="Start"/>
</form>
</div>
</body>
</html>'''
