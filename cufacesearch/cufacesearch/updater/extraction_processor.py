from __future__ import print_function
import sys
import time
import json
import math
import traceback
from datetime import datetime
from argparse import ArgumentParser
from cufacesearch.common.error import full_trace_error
from cufacesearch.common.conf_reader import ConfReader
from cufacesearch.indexer.hbase_indexer_minimal import HBaseIndexerMinimal, update_str_started, update_str_processed, \
                                                       img_buffer_column, img_URL_column
from cufacesearch.extractor.generic_extractor import DaemonBatchExtractor, GenericExtractor, build_extr_str
from cufacesearch.ingester.generic_kafka_processor import GenericKafkaProcessor

default_extr_proc_prefix = "EXTR_"

# Look for and process batch of a given extraction that have not been processed yet.
# Should be multi-threaded but single process...
def build_batch(list_in, batch_size):
  l = len(list_in)
  ibs = int(batch_size)
  for ndx in range(0, l, ibs):
    yield list_in[ndx:min(ndx + ibs, l)]

class ExtractionProcessor(ConfReader):

  def __init__(self, global_conf, prefix=default_extr_proc_prefix):
    self.extractor = None
    self.nb_empt = 0
    self.nb_err = 0

    super(ExtractionProcessor, self).__init__(global_conf, prefix)

    self.input_type = self.get_required_param("input_type")
    self.nb_threads = self.get_required_param("nb_threads")
    self.featurizer_type = self.get_required_param("featurizer_type")
    self.featurizer_prefix = self.get_required_param("featurizer_prefix")
    self.detector_type = self.get_required_param("detector_type")

    # Means we extract feature from the whole image
    if self.detector_type == "full":
      self.detector = None

    self.verbose = 0
    verbose = self.get_param("verbose")
    if verbose:
      self.verbose = int(verbose)

    # Need to be build from extraction type and detection input + "_processed"
    self.extr_family_column = "ext"
    tmp_extr_family_column = self.get_param("extr_family_column")
    if tmp_extr_family_column:
      self.extr_family_column = tmp_extr_family_column

    self.extr_prefix = build_extr_str(self.featurizer_type, self.detector_type, self.input_type)
    self.set_pp()

    # Initialize queues
    from multiprocessing import JoinableQueue as Queue
    self.q_in = []
    self.q_out = []
    for i in range(self.nb_threads):
      self.q_in.append(Queue(0))
      self.q_out.append(Queue(0))

    # Initialize extractors only once
    self.extractors = []
    for i in range(self.nb_threads):
      self.extractors.append(GenericExtractor(self.detector_type, self.featurizer_type, self.input_type,
                                      self.extr_family_column, self.featurizer_prefix, self.global_conf))

    # Beware, the self.extr_family_column should be added to the indexer families parameter in get_create_table...
    # What if the table has some other column families?...
    self.tablesha1_col_families = {'info': dict(), self.extr_family_column: dict()}

    # Initialize indexer
    self.indexer = HBaseIndexerMinimal(self.global_conf, prefix=self.get_required_param("indexer_prefix"))
    self.last_update_date_id = ''

    # Initialize ingester
    self.ingester = GenericKafkaProcessor(self.global_conf, prefix=self.get_required_param("proc_ingester_prefix"))
    self.ingester.pp = "ep"


  def set_pp(self):
    self.pp = "ExtractionProcessor"
    if self.extractor:
      self.pp += "_"+self.extr_prefix

  # Deprecated
  # def get_batch_hbase(self):
  #   # legacy implementation: better to have a kafka topic for batches to be processed to allow
  #   #       safe parallelization on different machines
  #   # NB: something similar to this, with a modification to get_unprocessed_updates_from_date to get updates that were
  #   # started a long time ago but never finisher could serve to pick up failed updates...
  #   try:
  #     # needs to read update table rows starting with 'index_update_'+extr and not marked as indexed.
  #     list_updates = self.indexer.get_unprocessed_updates_from_date(self.last_update_date_id, extr_type="_"+self.extr_prefix)
  #     if list_updates:
  #       for update_id, update_cols in list_updates:
  #         str_list_sha1s = update_cols[column_list_sha1s]
  #         list_sha1s = str_list_sha1s.split(',')
  #         print ("[{}.get_batch: log] Update {} has {} images.".format(self.pp, update_id, len(list_sha1s)))
  #         # also get 'ext:' to check if extraction was already processed?
  #         rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=["info:img_buffer"])
  #         #print "rows_batch", rows_batch
  #         if rows_batch:
  #           print("[{}.get_batch: log] Yielding for update: {}".format(self.pp, update_id))
  #           yield rows_batch, update_id
  #           print("[{}.get_batch: log] After yielding for update: {}".format(self.pp, update_id))
  #           self.last_update_date_id = '_'.join(update_id.split('_')[-2:])
  #         else:
  #           print("[{}.get_batch: log] Did not get any image buffers for the update: {}".format(self.pp, update_id))
  #     else:
  #       print("[{}.get_batch: log] Nothing to update!".format(self.pp))
  #   except Exception as inst:
  #     full_trace_error("[{}.get_batch: error] {}".format(self.pp, inst))

  def get_batch_kafka(self):
    # Read from a kafka topic to allow safer parallelization on different machines
    try:
      # Needs to read topic to get update_id and list of sha1s
      for msg in self.ingester.consumer:
        msg_dict = json.loads(msg.value)
        update_id = msg_dict.keys()[0]
        str_list_sha1s = msg_dict[update_id]
        list_sha1s = str_list_sha1s.split(',')
        print ("[{}.get_batch: log] Update {} has {} images.".format(self.pp, update_id, len(list_sha1s)))
        # NB: we could also get 'ext:' to double check if extraction was already processed
        #rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=["info:img_buffer"])
        rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=[img_buffer_column, img_URL_column])
        #print "rows_batch", rows_batch
        if rows_batch:
          print("[{}.get_batch: log] Yielding for update: {}".format(self.pp, update_id))
          yield rows_batch, update_id
          print("[{}.get_batch: log] After yielding for update: {}".format(self.pp, update_id))
          self.last_update_date_id = '_'.join(update_id.split('_')[-2:])
        else:
          print("[{}.get_batch: log] Did not get any image buffers for the update: {}".format(self.pp, update_id))
      else:
        print("[{}.get_batch: log] Nothing to update!".format(self.pp))
    except Exception as inst:
      full_trace_error("[{}.get_batch: error] {}".format(self.pp, inst))


  def process_batch(self):
    # Get a new update batch
    #for rows_batch, update_id in self.get_batch_hbase():
    for rows_batch, update_id in self.get_batch_kafka():
      try:
        start_update = time.time()
        self.nb_empt = 0
        print("[{}.process_batch: log] Processing update: {}".format(self.pp, update_id))
        sys.stdout.flush()

        threads = []

        # Mark batch as started to be process
        update_started_dict = {update_id: {'info:' + update_str_started: datetime.now().strftime('%Y-%m-%d:%H.%M.%S')}}
        self.indexer.push_dict_rows(dict_rows=update_started_dict, table_name=self.indexer.table_updateinfos_name)

        # Push images to queue
        list_in = []
        for img in rows_batch:
          # should decode base64
          if img_buffer_column in img[1]:
            tup = (img[0], img[1][img_buffer_column], False)
          else:
            # need to re-download
            if img_URL_column in img[1]:
              # download image
              from ..imgio.imgio import get_buffer_from_URL, buffer_to_B64
              img_buffer_b64 = buffer_to_B64(get_buffer_from_URL(img[1][img_URL_column]))
              tup = (img[0], img_buffer_b64, True)
            else:
              print("[{}.process_batch: warning] No buffer and no URL for image {} !".format(self.pp, img[0]))
              continue
          list_in.append(tup)
        q_batch_size = int(math.ceil(float(len(list_in))/self.nb_threads))
        for i, q_batch in enumerate(build_batch(list_in, q_batch_size)):
          self.q_in[i].put(q_batch)

        q_in_size = []
        q_in_size_tot = 0
        for i in range(self.nb_threads):
          q_in_size.append(self.q_in[i].qsize())
          q_in_size_tot += q_in_size[i]
        if self.verbose > 1:
          print("[{}.process_batch: log] Total input queues sizes is: {}".format(self.pp, q_in_size_tot))

        # Start daemons...
        for i in range(self.nb_threads):
          # one per non empty input queue
          if q_in_size[i] > 0:
            thread = DaemonBatchExtractor(self.extractors[i], self.q_in[i], self.q_out[i], verbose=self.verbose)
            thread.start()
            threads.append(thread)

        start_process = time.time()
        # Wait for all tasks to be marked as done
        for i in range(self.nb_threads):
          if q_in_size[i] > 0:
            # This would block forever if subprocess crashed?...
            self.q_in[i].join()

        # Gather results
        q_out_size = []
        q_out_size_tot = 0
        for i in range(self.nb_threads):
          q_out_size.append(self.q_out[i].qsize())
          q_out_size_tot += q_out_size[i]

        if self.verbose > 1:
          print("[{}.process_batch: log] Total output queues size is: {}".format(self.pp, q_out_size_tot))

        dict_imgs = dict()
        for i in range(self.nb_threads):
          while not self.q_out[i].empty():
            batch_out = self.q_out[i].get()
            for sha1, dict_out in batch_out:
              dict_imgs[sha1] = dict_out
            self.q_out[i].task_done()

        if self.verbose > 1:
          print_msg = "[{}.process_batch: log] Got features for {} images in {}s."
          print(print_msg.format(self.pp, len(dict_imgs.keys()), time.time() - start_process))
          sys.stdout.flush()

        # Push them
        self.indexer.push_dict_rows(dict_rows=dict_imgs, table_name=self.indexer.table_sha1infos_name)

        # Mark batch as processed
        update_processed_dict = {update_id: {'info:' + update_str_processed: datetime.now().strftime('%Y-%m-%d:%H.%M.%S')}}
        self.indexer.push_dict_rows(dict_rows=update_processed_dict, table_name=self.indexer.table_updateinfos_name)

        # Cleanup
        for th in threads:
          del th

        print_msg = "[{}.process_batch: log] Completed update {} in {}s."
        print(print_msg.format(self.pp, update_id, time.time() - start_update))
        sys.stdout.flush()
        self.nb_err = 0

      except Exception as inst:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fulltb = traceback.format_tb(exc_tb)
        raise type(inst)(" {} ({})".format(inst, ''.join(fulltb)))

  def run(self):
    self.nb_empt = 0
    self.nb_err = 0
    while True:
      try:
        self.process_batch()
        print("[ExtractionProcessor: log] Nothing to process at: {}".format(datetime.now().strftime('%Y-%m-%d:%H.%M.%S')))
        sys.stdout.flush()
        time.sleep(10*self.nb_empt)
        self.nb_empt += 1
      except Exception as inst:
        print("ExtractionProcessor died: {} {}".format(type(inst), inst))
        sys.stdout.flush()
        time.sleep(10 * self.nb_err)
        self.nb_err += 1


if __name__ == "__main__":

  # Get conf file
  parser = ArgumentParser()
  parser.add_argument("-c", "--conf", dest="conf_file", required=True)
  parser.add_argument("-p", "--prefix", dest="prefix", default=default_extr_proc_prefix)
  options = parser.parse_args()

  # Initialize extraction processor
  ep = ExtractionProcessor(options.conf_file, prefix=options.prefix)
  nb_err = 0

  print("Starting extraction {}".format(ep.extr_prefix))
  while True:
    try:
      ep.run()
      nb_err = 0
    except Exception as inst:
      full_trace_error("Extraction processor failed: {}".format(inst))
      time.sleep(10*nb_err)
      nb_err += 1