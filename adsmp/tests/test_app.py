#!/usr/bin/env python
# -*- coding: utf-8 -*-



import sys
import os

import unittest
import json
import re
import os
import math
import mock
import adsputils
from mock import patch
from io import BytesIO
from datetime import datetime
from adsmp import app, models
from adsmp.models import Base, MetricsBase
import testing.postgresql

class TestAdsOrcidCelery(unittest.TestCase):
    """
    Tests the appliction's methods
    """
    
    @classmethod
    def setUpClass(cls):
        cls.postgresql = \
            testing.postgresql.Postgresql(host='127.0.0.1', port=15678, user='postgres', 
                                          database='test')

    @classmethod
    def tearDownClass(cls):
        cls.postgresql.stop()
        

    def setUp(self):
        unittest.TestCase.setUp(self)
        
        proj_home = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
        self.app = app.ADSMasterPipelineCelery('test', local_config=\
            {
            'SQLALCHEMY_URL': 'sqlite:///',
            'METRICS_SQLALCHEMY_URL': 'postgresql://postgres@127.0.0.1:15678/test',
            'SQLALCHEMY_ECHO': False,
            'PROJ_HOME' : proj_home,
            'TEST_DIR' : os.path.join(proj_home, 'adsmp/tests'),
            })
        Base.metadata.bind = self.app._session.get_bind()
        Base.metadata.create_all()
        
        MetricsBase.metadata.bind = self.app._metrics_engine
        MetricsBase.metadata.create_all()

    
    def tearDown(self):
        unittest.TestCase.tearDown(self)
        Base.metadata.drop_all()
        MetricsBase.metadata.drop_all()
        self.app.close_app()

    
    def test_app(self):
        assert self.app._config.get('SQLALCHEMY_URL') == 'sqlite:///'
        assert self.app.conf.get('SQLALCHEMY_URL') == 'sqlite:///'


    def test_update_processed_timestamp(self):
        self.app.update_storage('abc', 'bib_data', {'bibcode': 'abc', 'hey': 1})
        self.app.update_processed_timestamp('abc', 'solr')
        with self.app.session_scope() as session:
            r = session.query(models.Records).filter_by(bibcode='abc').first()
            self.assertFalse(r.processed)
            self.assertFalse(r.metrics_processed)
            self.assertTrue(r.solr_processed)
        self.app.update_processed_timestamp('abc', 'metrics')
        with self.app.session_scope() as session:
            r = session.query(models.Records).filter_by(bibcode='abc').first()
            self.assertFalse(r.processed)
            self.assertTrue(r.metrics_processed)
            self.assertTrue(r.solr_processed)
        self.app.update_processed_timestamp('abc')
        with self.app.session_scope() as session:
            r = session.query(models.Records).filter_by(bibcode='abc').first()
            self.assertTrue(r.processed)
            self.assertTrue(r.metrics_processed)
            self.assertTrue(r.solr_processed)
        
    def test_mark_processed(self):
        self.app.mark_processed(['abc'], 'solr')
        r = self.app.get_record('abc')
        self.assertEquals(r, None)
        
        self.app.update_storage('abc', 'bib_data', {'bibcode': 'abc', 'hey': 1})
        self.app.mark_processed(['abc'], 'solr')
        r = self.app.get_record('abc')
        
        self.assertTrue(r['solr_processed'])
        self.assertFalse(r['status'])

        self.app.mark_processed(['abc'], None, status='solr-failed')
        r = self.app.get_record('abc')
        self.assertTrue(r['solr_processed'])
        self.assertTrue(r['processed'])
        self.assertEquals(r['status'], 'solr-failed')


    def test_reindex(self):
        self.app.update_storage('abc', 'bib_data', {'bibcode': 'abc', 'hey': 1})
        self.app.update_storage('foo', 'bib_data', {'bibcode': 'foo', 'hey': 1})
        
        with mock.patch('adsmp.solr_updater.update_solr', return_value=[200]):
            failed = self.app.reindex([{'bibcode': 'abc'}, {'bibcode': 'foo'}], ['http://solr1'])
            self.assertTrue(len(failed) == 0)
            with self.app.session_scope() as session:
                for x in ['abc', 'foo']:
                    r = session.query(models.Records).filter_by(bibcode=x).first()
                    self.assertFalse(r.processed)
                    self.assertFalse(r.metrics_processed)
                    self.assertTrue(r.solr_processed)
                    
        # pretend failure
        with mock.patch('adsmp.solr_updater.update_solr', return_value=[503]) as us, \
             mock.patch.object(self.app, 'update_processed_timestamp') as upt:
            failed = self.app.reindex([{'bibcode': 'abc'}, {'bibcode': 'foo'}], ['http://solr1'])
            self.assertTrue(len(failed) == 0)
            self.assertEqual(str(upt.call_args_list), "[call('abc', type=u'solr'), call('foo', type=u'solr')]")
            self.assertEqual(us.call_count, 3)
            self.assertEqual(str(us.call_args_list[-1]), "call([{'bibcode': 'foo'}], ['http://solr1'], commit=False, ignore_errors=False)") 


    def test_update_metrics(self):
        self.app.update_storage('abc', 'metrics', {
                     'author_num': 1,
                     'bibcode': 'abc',
                    })
        self.app.update_storage('foo', 'metrics', {
                    'bibcode': 'foo', 
                    'citation_num': 6
                    })
        
        batch_insert = [self.app.get_record('abc')['metrics']]
        batch_update = [self.app.get_record('foo')['metrics']]
        
        bibc, errs = self.app.update_metrics_db(batch_insert, batch_update)
        self.assertEquals(bibc, ['abc', 'foo'])
        
        for x in ['abc', 'foo']:
            r = self.app.get_record(x)
            self.assertFalse(r['processed'])
            self.assertTrue(r['metrics_processed'])
            self.assertFalse(r['solr_processed'])


    def test_update_records(self):
        """Makes sure we can write recs into the storage."""
        now = adsputils.get_date()
        last_time = adsputils.get_date()
        for k in ['bib_data', 'nonbib_data', 'orcid_claims']:
            self.app.update_storage('abc', k, {'foo': 'bar', 'hey': 1})
            with self.app.session_scope() as session:
                r = session.query(models.Records).filter_by(bibcode='abc').first()
                self.assertTrue(r.id == 1)
                j = r.toJSON()
                self.assertEquals(j[k], {'foo': 'bar', 'hey': 1})
                t = j[k + '_updated']
                self.assertTrue(now < t)
                self.assertTrue(last_time < j['updated'])
                last_time = j['updated']
        
        self.app.update_storage('abc', 'fulltext', 'foo bar')
        with self.app.session_scope() as session:
            r = session.query(models.Records).filter_by(bibcode='abc').first()
            self.assertTrue(r.id == 1)
            j = r.toJSON()
            self.assertEquals(j['fulltext'], u'foo bar')
            t = j['fulltext_updated']
            self.assertTrue(now < t)
        
        r = self.app.get_record('abc')
        self.assertEquals(r['id'], 1)
        self.assertEquals(r['processed'], None)
        
        r = self.app.get_record(['abc'])
        self.assertEquals(r[0]['id'], 1)
        self.assertEquals(r[0]['processed'], None)
        
        r = self.app.get_record('abc', load_only=['id'])
        self.assertEquals(r['id'], 1)
        self.assertFalse('processed' in r)
        
        self.app.update_processed_timestamp('abc')
        r = self.app.get_record('abc')
        self.assertTrue(r['processed'] > now)
        
        # now delete it
        self.app.delete_by_bibcode('abc')
        r = self.app.get_record('abc')
        self.assertTrue(r is None)
        with self.app.session_scope() as session:
            r = session.query(models.ChangeLog).filter_by(key='bibcode:abc').first()
            self.assertTrue(r.key, 'abc')
            
        
    def test_rename_bibcode(self):
        self.app.update_storage('abc', 'metadata', {'foo': 'bar', 'hey': 1})
        r = self.app.get_record('abc')
        
        self.app.rename_bibcode('abc', 'def')
        
        with self.app.session_scope() as session:
            ref = session.query(models.IdentifierMapping).filter_by(key='abc').first()
            self.assertTrue(ref.target, 'def')
            
        self.assertTrue(self.app.get_changelog('abc'), [{'target': u'def', 'key': u'abc'}])

    
if __name__ == '__main__':
    unittest.main()
