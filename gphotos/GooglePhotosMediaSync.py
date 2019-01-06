#!/usr/bin/env python2
# coding: utf8
import os.path

from . import Utils
from .GooglePhotosMedia import GooglePhotosMedia
from .GoogleMedia import MediaType
from .LocalData import LocalData
from .DatabaseMedia import DatabaseMedia
import logging
import requests
import shutil

log = logging.getLogger('gphotos.Photos')


class NoGooglePhotosFolderError(Exception):
    pass


class GooglePhotosMediaSync(object):
    PAGE_SIZE = 100

    def __init__(self, root_folder, db, api):
        """
        :param (str) root_folder:
        :param (LocalData) db:
        """
        self._db = db
        self._root_folder = root_folder
        self.api = api

        self._latest_download = Utils.minimum_date()
        # public members to be set after init
        self.startDate = None
        self.endDate = None
        self.includeVideo = False

    @property
    def latest_download(self):
        return self._latest_download

    # todo this will currently do nothing unless using --flush-db
    # need to look at drive changes api if we want to support deletes and
    # incremental backup.
    def check_for_removed(self):
        # note for partial scans using date filters this is still OK because
        # for a file to exist it must have been indexed in a previous scan
        log.info(u'Finding deleted media ...')
        top_dir = os.path.join(self._root_folder, GooglePhotosMedia.MEDIA_FOLDER)
        for (dir_name, _, file_names) in os.walk(top_dir):
            for file_name in file_names:
                file_row = self._db.get_file_by_path(dir_name, file_name)
                if not file_row:
                    name = os.path.join(dir_name, file_name)
                    os.remove(name)
                    log.warning("%s deleted", name)

    def write_media_index(self, media, update=True):
        media.save_to_db(self._db, update)
        if media.modify_date > self._latest_download:
            self._latest_download = media.modify_date

    PHOTO_QUERY = u"mimeType contains 'image/' and trashed=false"
    VIDEO_QUERY = u"(mimeType contains 'image/' or mimeType contains " \
                  u"'video/') and trashed=false"
    AFTER_QUERY = u" and modifiedDate >= '{}T00:00:00'"
    BEFORE_QUERY = u" and modifiedDate <= '{}T00:00:00'"
    FILENAME_QUERY = u'title contains "{}" and trashed=false'

    # these are the queries I'd like to use but they are for drive api v3
    AFTER_QUERY2 = u" and (modifiedTime >= '{0}T00:00:00' or " \
                   u"createdTime >= '{0}T00:00:00') "
    BEFORE_QUERY2 = u" and (modifiedTime <= '{0}T00:00:00' or " \
                    u"createdTime <= '{0}T00:00:00') "

    def index_photos_media(self):
        log.info(u'Indexing Google Photos Files ...')
        #
        # if self.includeVideo:
        #     q = self.VIDEO_QUERY
        # else:
        #     q = self.PHOTO_QUERY
        #
        # if self.startDate:
        #     q += self.AFTER_QUERY.format(self.startDate)
        # else:
        #     # setup for incremental backup
        #     (self._latest_download, _) = self._db.get_scan_dates()
        #     if not self._latest_download:
        #         self._latest_download = Utils.minimum_date()
        #     else:
        #         s = Utils.date_to_string(self._latest_download, True)
        #         q += self.AFTER_QUERY.format(s)
        # if self.endDate:
        #     q += self.BEFORE_QUERY.format(self.endDate)

        # count = 0
        # r = self.api.mediaItems.list.execute(pageSize=100)
        # while r:
        #     results = r.json()
        #     for a in results['mediaItems']:
        #         count += 1
        #         type_description = a.get('mimeType')
        #         if type_description not in ['image/jpeg', 'video/mp4', 'image/gif']:
        #             print(count, a.get('filename'), type_description, a.get('description'))
        #
        #     next_page = results.get('nextPageToken')
        #     if next_page:
        #         r = self.api.mediaItems.list.execute(pageSize=100, pageToken=next_page)
        #     else:
        #         break

        count = 0
        try:
            response = self.api.mediaItems.list.execute(pageSize=100)
            while response:
                items = response.json()
                for media_item_json in items['mediaItems']:
                    media_item = GooglePhotosMedia(media_item_json)
                    media_item.set_path_by_date()
                    row = media_item.is_indexed(self._db)
                    if not row:
                        count += 1
                        log.info(u"Added %d %s", count, media_item.relative_path)
                        self.write_media_index(media_item, False)
                        if count % 1000 == 0:
                            self._db.store()
                    elif media_item.modify_date > row.ModifyDate:
                        log.info(u"Updated %d %s", count, media_item.relative_path)
                        self.write_media_index(media_item, True)
                    else:
                        log.debug \
                            (u"Skipped %d %s", count, media_item.relative_path)
                next_page = items.get('nextPageToken')
                if next_page:
                    response = self.api.mediaItems.list.execute(pageSize=100, pageToken=next_page)
                else:
                    break
        finally:
            # store latest date for incremental backup only if scanning all
            if not (self.startDate or self.endDate):
                self._db.set_scan_dates(drive_last_date=self._latest_download)

    @classmethod
    def download_file(cls, url, local_path):
        r = requests.get(url, stream=True)
        with open(local_path, 'wb') as f:
            shutil.copyfileobj(r.raw, f)

    def download_photo_media(self):
        log.info(u'Downloading Photos ...')
        count = 0
        # noinspection PyTypeChecker
        for media_item in DatabaseMedia.get_media_by_search(self._db, media_type=MediaType.PHOTOS,
                                                            start_date=self.startDate, end_date=self.endDate):
            local_folder = os.path.join(self._root_folder, media_item.relative_folder)
            local_full_path = os.path.join(local_folder, media_item.filename)
            if os.path.exists(local_full_path):
                log.info(u'skipping {} ...'.format(local_full_path))
                # todo is there anyway to detect remote updates with photos API?
                # if Utils.to_timestamp(media.modify_date) > \
                #         os.path.getctime(local_full_path):
                #     log.warning(u'{} was modified'.format(local_full_path))
                # else:
                continue

            if not os.path.isdir(local_folder):
                os.makedirs(local_folder)
            temp_filename = os.path.join(self._root_folder, '.temp-photo')

            count += 1
            try:
                response = self.api.mediaItems.get.execute(mediaItemId=str(media_item.id))
                r_json = response.json()
                if media_item.is_video():
                    log.info(u'downloading video {} {} ...'.format(count, local_full_path))
                    download_url = '{}=dv'.format(r_json['baseUrl'])
                else:
                    log.info(u'downloading image {} {} ...'.format(count, local_full_path))
                    download_url = '{}=d'.format(r_json['baseUrl'])
                self.download_file(download_url, temp_filename)
                os.rename(temp_filename, local_full_path)
                # set the access date to create date since there is nowhere
                # else to put it on linux (and is useful for debugging)
                os.utime(local_full_path,
                         (Utils.to_timestamp(media_item.modify_date),
                          Utils.to_timestamp(media_item.create_date)))
            except Exception as e:
                log.error('failure downloading {}.\n{}{}'.format(local_full_path, type(e), e))
