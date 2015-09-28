import os
import mimetypes

import botocore.session

from pulsar.apps.greenio import GreenPool, getcurrent
from .sock import wrap_poolmanager, StreamingBodyWsgiIterator

# 8MB for multipart uploads
MULTI_PART_SIZE = 2**23


class Botocore(object):
    '''An asynchronous wrapper for botocore
    '''
    def __init__(self, service_name, region_name=None,
                 endpoint_url=None, session=None, green_pool=None,
                 green=True, **kwargs):
        self._green_pool = green_pool
        self.session = session or botocore.session.get_session()
        self.client = self.session.create_client(service_name,
                                                 region_name=region_name,
                                                 endpoint_url=endpoint_url,
                                                 **kwargs)
        if green:
            endpoint = self.client._endpoint
            for adapter in endpoint.http_session.adapters.values():
                adapter.poolmanager = wrap_poolmanager(adapter.poolmanager)

            self._make_api_call = self.client._make_api_call
            self.client._make_api_call = self._call

    def __getattr__(self, operation):
        return getattr(self.client, operation)

    def green_pool(self):
        if not self._green_pool:
            self._green_pool = GreenPool()
        return self._green_pool

    def wsgi_stream_body(self, body, n=-1):
        '''WSGI iterator of a botocore StreamingBody
        '''
        return StreamingBodyWsgiIterator(body, self.green_pool(), n)

    def upload_file(self, bucket, filename, uploadpath=None,
                    ContentType=None, **kw):
        '''Upload a file to S3 using the multi-part uploader
        '''
        size = os.stat(filename).st_size
        key = os.path.basename(filename)
        if uploadpath:
            if not uploadpath.endswith('/'):
                uploadpath = '%s/' % uploadpath
            key = '%s%s' % (uploadpath, key)

        params = dict(Bucket=bucket, Key=key)
        if not ContentType:
            ContentType, _ = mimetypes.guess_type(filename)

        if ContentType:
            params['ContentType'] = ContentType

        if size > MULTI_PART_SIZE:
            return self._multipart(filename, params)
        else:
            with open(filename, 'rb') as file:
                params['Body'] = file.read()
            return self.put_object(**params)

    def _call(self, operation, kwargs):
        if getcurrent().parent:
            return self._make_api_call(operation, kwargs)
        else:
            pool = self.green_pool()
            return pool.submit(self._make_api_call, operation, kwargs)

    def _read_body(self, body, n):
        return

    def _multipart(self, filename, params):
        response = self.create_multipart_upload(**params)
        bucket = params['Bucket']
        key = params['Key']
        uid = response['UploadId']
        params['UploadId'] = uid
        params.pop('ContentType', None)
        try:
            parts = []
            with open(filename, 'rb') as file:
                while True:
                    body = file.read(MULTI_PART_SIZE)
                    if not body:
                        break
                    num = len(parts) + 1
                    params['Body'] = body
                    params['PartNumber'] = num
                    part = self.upload_part(**params)
                    parts.append(dict(ETag=part['ETag'], PartNumber=num))
        except:
            self.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=uid)
            raise
        else:
            if parts:
                all = dict(Parts=parts)
                return self.complete_multipart_upload(
                    Bucket=bucket, UploadId=uid, Key=key, MultipartUpload=all)
            else:
                self.abort_multipart_upload(Bucket=bucket, Key=key,
                                            UploadId=uid)
