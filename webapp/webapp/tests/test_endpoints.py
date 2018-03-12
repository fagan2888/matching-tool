import unittest
import re
import json
import requests_mock
from webapp.tests.utils import rig_all_the_things, rig_test_client_with_engine,\
    authenticate,\
    create_and_populate_matched_table, load_json_example
from datetime import date
from smart_open import smart_open
from webapp.database import db_session
from webapp.models import Upload, MergeLog
import unicodecsv as csv
import pandas as pd

GOOD_HMIS_FILE = 'sample_data/uploader_input/hmis_service_stays/good.csv'
HMIS_FILE_WITH_DUPLICATES = 'sample_data/uploader_input/hmis_service_stays/duplicates.csv'
BOOKINGS_FILE = 'sample_data/uploader_input/jail_bookings/missing-fields.csv'
MATCHED_BOOKING_FILE = 'sample_data/matched/matched_bookings_data_20171207.csv'
MATCHED_HMIS_FILE = 'sample_data/matched/matched_hmis_data_20171207.csv'


class GetMatchedResultsCase(unittest.TestCase):
    def test_matched_results(self):
        with rig_test_client_with_engine() as (app, engine):
            authenticate(app)
            # Create matched jail_bookings
            table_name = 'jail_bookings'
            create_and_populate_matched_table(table_name, MATCHED_BOOKING_FILE, engine)
            # Create matched hmis_service_stays
            table_name = 'hmis_service_stays'
            create_and_populate_matched_table(table_name, MATCHED_HMIS_FILE, engine)
            response = app.get(
                '/api/chart/get_schema?start=2017-12-01&end=2018-01-01',
            )
            self.assertEqual(response.status_code, 200)

            response_data = json.loads(response.get_data().decode('utf-8'))
            expected_data = load_json_example('sample_data/results_input/results_12012017_01012018.json')
            self.assertDictEqual(response_data['results']['filters'], expected_data['results']['filters'])

            set_of_venn_size = lambda venn: set(map(lambda x: x['size'], venn))
            self.assertEqual(set_of_venn_size(response_data['results']['vennDiagramData']), set_of_venn_size(expected_data['results']['vennDiagramData']))

            self.assertDictEqual(response_data, expected_data)


class UploadFileTestCase(unittest.TestCase):
    def test_good_file(self):
        with rig_all_the_things() as app:
            response = app.post(
                '/api/upload/upload_file?jurisdiction=boone&eventType=hmis_service_stays',
                content_type='multipart/form-data',
                data={'file_field': (open(GOOD_HMIS_FILE, 'rb'), 'myfile.csv')}
            )
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert response_data['status'] == 'validating'
            assert 'jobKey' in response_data
            assert 'message' in response_data

            job_key = response_data['jobKey']

            # get validation result and upload to s3
            response = app.get(
                '/api/upload/validated_result/' + job_key
            )
            response_data = json.loads(response.get_data().decode('utf-8'))

            assert 'validation' in response_data
            assert response_data['validation']['status'] == 'valid'
            assert response_data['validation']['jobKey'] == job_key

            assert 'upload_result' in response_data
            assert 'rowCount' in response_data['upload_result']
            assert 'exampleRows' in response_data['upload_result']
            assert 'uploadId' in response_data['upload_result']
            assert 'fieldOrder' in response_data['upload_result']

            current_date = date.today().isoformat()
            expected_s3_path = 's3://test-bucket/boone/hmis_service_stays/uploaded/{}/{}'.format(current_date, response_data['upload_result']['uploadId'])
            with smart_open(expected_s3_path) as expected_s3_file:
                with smart_open(GOOD_HMIS_FILE) as source_file:
                    # we do not expect the file on s3 to be the same as the
                    # uploaded source file - missing columns should be filled in
                    s3_df = pd.read_csv(expected_s3_file)
                    source_df = pd.read_csv(source_file, sep='|')
                    assert source_df.equals(s3_df[source_df.columns.tolist()])

            assert db_session.query(Upload).filter(Upload.id == response_data['upload_result']['uploadId']).one

    def test_file_with_duplicates(self):
        with rig_all_the_things() as app:
            response = app.post(
                '/api/upload/upload_file?jurisdiction=boone&eventType=hmis_service_stays',
                content_type='multipart/form-data',
                data={'file_field': (open(HMIS_FILE_WITH_DUPLICATES, 'rb'), 'myfile.csv')}
            )
            response_data = json.loads(response.get_data().decode('utf-8'))
            job_key = response_data['jobKey']
            assert response_data['status'] == 'validating'

            response = app.get('/api/upload/validated_result/' + job_key)
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert 'validation' in response_data
            assert response_data['validation']['status'] == 'invalid'
            assert 'duplicate primary key' in response_data['upload_result']['errorReport'][0]['message']


class MergeFileTestCase(unittest.TestCase):
    def do_upload(self, app, request_mock):
        # to set up, we want a raw file to have been uploaded
        # present in s3 and metadata in the database
        # use the upload file endpoint as a shortcut for setting
        # this environment up quickly, though this is not ideal
        request_mock.get(re.compile('/match/boone/hmis_service_stays'), text='stuff')
        response = app.post(
            '/api/upload/upload_file?jurisdiction=boone&eventType=hmis_service_stays',
            content_type='multipart/form-data',
            data={'file_field': (open(GOOD_HMIS_FILE, 'rb'), 'myfile.csv')}
        )
        # parse and assert the response just in case something
        # breaks before the merge stage it will show itself here
        # and not make us think the merge is broken
        response_data = json.loads(response.get_data().decode('utf-8'))
        job_key = response_data['jobKey']
        assert response_data['status'] == 'validating'
        response = app.get('/api/upload/validated_result/' + job_key)
        response_data = json.loads(response.get_data().decode('utf-8'))
        assert response_data['validation']['status'] == 'valid'
        upload_id = response_data['upload_result']['uploadId']
        compiled_regex = re.compile('/match/boone/hmis_service_stays\?uploadId={upload_id}'.format(upload_id=upload_id))
        request_mock.get(compiled_regex, text='stuff')
        return upload_id

    @requests_mock.mock()
    def test_good_file(self, request_mock):
        with rig_all_the_things() as app:
            upload_id = self.do_upload(app, request_mock)
            # okay, here's what we really want to test.
            # call the merge endpoint
            response = app.post('/api/upload/merge_file?uploadId={}'.format(upload_id))
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert response_data['status'] == 'valid'
            # make sure that there is a new merged file on s3
            expected_s3_path = 's3://test-bucket/boone/hmis_service_stays/merged'
            with smart_open(expected_s3_path, 'rb') as expected_s3_file:
                reader = csv.reader(expected_s3_file)
                assert len([row for row in reader]) == 11

            # and make sure that the merge log has a record of this
            assert db_session.query(MergeLog).filter(MergeLog.upload_id == '123-456').one

    @requests_mock.mock()
    def test_error_transaction(self, request_mock):
        with rig_all_the_things() as app:
            upload_id = self.do_upload(app, request_mock)
            # try and merge an id that doesn't exist, should cause error
            response = app.post('/api/upload/merge_file?uploadId={}'.format('garbage'))
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert response_data['status'] == 'error'
            # now merge the right one, the db should not be in a weird state
            response = app.post('/api/upload/merge_file?uploadId={}'.format(upload_id))
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert response_data['status'] == 'valid'


class MergeBookingsFileTestCase(unittest.TestCase):
    def do_upload(self, app, request_mock):
        # to set up, we want a raw file to have been uploaded
        # present in s3 and metadata in the database
        # use the upload file endpoint as a shortcut for setting
        # this environment up quickly, though this is not ideal
        request_mock.get(re.compile('/match/boone/jail_bookings'), text='stuff')
        response = app.post(
            '/api/upload/upload_file?jurisdiction=boone&eventType=jail_bookings',
            content_type='multipart/form-data',
            data={'file_field': (open(BOOKINGS_FILE, 'rb'), 'myfile.csv')}
        )
        # parse and assert the response just in case something
        # breaks before the merge stage it will show itself here
        # and not make us think the merge is broken
        response_data = json.loads(response.get_data().decode('utf-8'))
        assert 'jobKey' in response_data, response_data
        job_key = response_data['jobKey']
        assert response_data['status'] == 'validating'
        response = app.get('/api/upload/validated_result/' + job_key)
        response_data = json.loads(response.get_data().decode('utf-8'))
        print(response_data)
        assert response_data['validation']['status'] == 'valid'
        upload_id = response_data['upload_result']['uploadId']
        compiled_regex = re.compile('/match/boone/jail_bookings\?uploadId={upload_id}'.format(upload_id=upload_id))
        request_mock.get(compiled_regex, text='stuff')
        return upload_id

    @requests_mock.mock()
    def test_good_file(self, request_mock):
        with rig_all_the_things() as app:
            upload_id = self.do_upload(app, request_mock)
            # okay, here's what we really want to test.
            # call the merge endpoint
            response = app.post('/api/upload/merge_file?uploadId={}'.format(upload_id))
            response_data = json.loads(response.get_data().decode('utf-8'))
            assert response_data['status'] == 'valid'
            # make sure that there is a new merged file on s3
            expected_s3_path = 's3://test-bucket/boone/jail_bookings/merged'
            with smart_open(expected_s3_path, 'rb') as expected_s3_file:
                reader = csv.reader(expected_s3_file)
                assert len([row for row in reader]) == 11

            # and make sure that the merge log has a record of this
            assert db_session.query(MergeLog).filter(MergeLog.upload_id == '123-456').one
