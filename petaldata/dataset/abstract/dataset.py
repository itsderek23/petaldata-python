import pandas as pd
import requests
import datetime
import os
from datetime import datetime
from datetime import date

import petaldata
from petaldata import util
from petaldata.storage import *

class Dataset(object):
  CSV_FILE_PREFIX = "dataset"

  def __init__(self,base_pickle_filename=None):
    """
    Initializes the Dataset, attempting to load a previously saved dataframe via `base_pickle_filename`.

    Parameters
    ----------
    base_pickle_filename : str
      The name of the Pickle file the Dataset should be loaded from and saved to. If the file doesn't exit, it will be created when
      the Dataset is saved. If not provided, `default_base_pickle_filename()` is used.
    """
    self.csv_filename = None
    self.df = None
    self.__metadata = None
    if base_pickle_filename == None: base_pickle_filename = self.default_base_pickle_filename()
    self.local = Local(base_pickle_filename)
    self.s3 = S3.if_enabled(base_pickle_filename)
    self._load_from_cache()

  def download(self,created_gt=None,_offset=None, limit=None):
    self._download_csv(created_gt,_offset,limit)
    self._load_from_download()

    if self.df is None:
      print("\tUnable to load dataframe.")
    return self.df

  # TODO - rename to merge?
  # https://en.wikipedia.org/wiki/Merge_(SQL)
  def update(self, created_gt=None):
    print("Updating...")
    if created_gt is None:
      print("\tSetting created_gt=",self.updated_at)
      created_gt = self.updated_at
    else:
      print("\tcreated >",created_gt)
    new = type(self)()
    new.download(created_gt=created_gt)
    new.load_from_download() # don't want to save a pickle! would override existing.
    self.upsert(new)
    return self

  def upsert(self,other_resource):
    old_count = self.df.shape[0]
    # only concat if rows found, otherwise dtypes can change
    # https://stackoverflow.com/questions/33001585/pandas-dataframe-concat-update-upsert
    if other_resource.df.shape[0] > 0:
      print("\tInserting new rows")
      print("created before concat:",self.df.created.dtype,self.df.created.head(5))
      print("other created before concat:",other_resource.df.created.dtype,other_resource.df.created.head(5))
      df = pd.concat([self.df, other_resource.df[~other_resource.df.index.isin(self.df.index)]])
      print("created after concat:",df.created.dtype,df.created.head(5))
      print("\tUpdating existing rows")
      df.update(other_resource.df)
      # timezone is stripped after update. add back.
      df = self.set_date_tz(df)
      print("created after set_date_z:",df.created.dtype,df.created.head(5))
      self.df = df

    new_count = self.df.shape[0] - old_count
    if new_count > 0:
      print("Added {} new rows. Now with {} total rows.".format(new_count, self.df.shape[0]))
      print("\tTime Range:",self.df[self.created].min(), "-", self.df[self.created].max())
    else:
      print("No new rows.")
    return self

  def reset(self):
    print("Resetting...")
    if self.local.enabled == True: self.local.delete()
    if self.s3: self.s3.delete()

    self.df = None
    self.__metadata = None
    return self

  def save(self):
    print("Saving to Pickle file...")
    if self.local.enabled == True: self.local.save(self)
    if self.s3: self.s3.save(self)

    return True
    
  @property
  def updated_at(self):
    return self.df[self.created].max()

  def request_params(self,created_gt,_offset):
    pass

  def default_base_pickle_filename(self):
    return self.CSV_FILE_PREFIX+".pkl"

  def _load_from_cache(self):
    print("Attempting to load saved pickle file...")
    if (self.local.enabled == True): self.df = self.local.read_pickle_dataframe() 
    if (self.s3 is not None): self.df = self.s3.read_pickle_dataframe() 

    if self.df is None:
      print("\tNo cached files exist.")

  def _download_csv(self,created_gt=None,_offset=None, limit=None):
    first_chunk = True
    start_time = datetime.now()
    filename = self.local.csv_download_filename(self.CSV_FILE_PREFIX+"_",start_time)
    print("Starting download to",filename,"...")
    with requests.get(self.api_url+".csv", headers=self.request_headers, params=self.request_params(created_gt,limit,_offset), stream=True) as r:
        r.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=None):
                # TODO - how to handle 500 errors in chunks?
                if chunk: # filter out keep-alive new chunks
                    if (first_chunk):
                        first_chunk = False
                        print("\t...will update progress every 25 MB of data transfer.")
                    f.write(chunk)
                    size_in_mb = Local.file_size_in_mb(filename)
                    if ((size_in_mb > 1) & (len(chunk) > 1000) & (size_in_mb % 25 == 0) ):
                        print("\tDownloaded", size_in_mb, "MB...")

    size_in_mb = Local.file_size_in_mb(filename)
    time_delta = datetime.now() - start_time
    print("\t...Done.\n\tFile Size=",size_in_mb,"MB." " Total Time=", 
          round(time_delta.seconds/60.0,2), "minutes", "\n\tLocation:", filename)
    self.csv_filename = filename
    return self.csv_filename

  def _load_from_download(self,filename=None):
    if filename is None:
      filename = self.csv_filename
    else:
      filename = self.local.dir + filename
    print("Loading {} MB CSV file...".format(Local.file_size_in_mb(filename)))
    dataframe = pd.read_csv(filename,parse_dates = self.metadata.get("convert_dates"))
    dataframe.set_index(self.metadata.get("index"),inplace=True)
    self.df = dataframe
    print("\t...Done. Dataframe Shape:",self.df.shape)
    count = self.df.shape[0]
    if ('created' in dataframe.columns) & (count > 0):
      print("\tTime Range:",self.df.created.min(), "-", self.df.created.max())
    return self.df

  @property
  def metadata(self):
    if self.__metadata is None:
      self.__metadata = self.get_metadata()

    return self.__metadata

  def get_metadata(self):
    r = requests.get(self.api_url+"/metadata.pandas",  headers=self.request_headers)
    r.raise_for_status()
    metadata = r.json()
    print("Loaded metadata w/keys=",list(metadata.keys()))
    print(metadata)
    return metadata

  @staticmethod
  def set_date_tz(dataframe):
    for col in dataframe.columns:
      # this is a tz-naive timestamp. w/a tz would look like "datetime64[ns, UTC]"
      if dataframe[col].dtype == 'datetime64[ns]':
        dataframe[col] = dataframe[col].dt.tz_localize('UTC')
        # Strips out tz info so don't have to worry about comparing tz-aware datetimes
        # dataframe[col] = dataframe[col].dt.tz_convert(None)
    return dataframe

  @property
  def api_url(self):
    return petaldata.api_base + self.RESOURCE_URL