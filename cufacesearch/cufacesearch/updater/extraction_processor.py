from __future__ import print_function
import gc
import sys
import time
import json
import math
import threading
import traceback
from datetime import datetime
from argparse import ArgumentParser
from cufacesearch.common import column_list_sha1s
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

class ThreadedDownloaderBufferOnly(threading.Thread):

  def __init__(self, q_in, q_out):
    threading.Thread.__init__(self)
    self.q_in = q_in
    self.q_out = q_out

  def run(self):
    from cufacesearch.imgio.imgio import get_buffer_from_URL, buffer_to_B64

    while self.q_in.empty() == False:
      try:
        # The queue should already have items, no need to block
        (sha1, url, push_back) = self.q_in.get(False)
      except:
        continue

      try:
        img_buffer = get_buffer_from_URL(url)
        if img_buffer:
          # Push
          self.q_out.put((sha1, buffer_to_B64(img_buffer), push_back, None))
      except Exception as inst:
        self.q_out.put((sha1, None, push_back, inst))

      # Mark as done
      self.q_in.task_done()

class ExtractionProcessor(ConfReader):

  def __init__(self, global_conf, prefix=default_extr_proc_prefix):
    self.extractor = None
    self.nb_empt = 0
    self.nb_err = 0
    self.max_proc_time = 600 # in seconds?

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
    self.init_queues()

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

  def init_queues(self):
    from multiprocessing import JoinableQueue
    self.q_in = []
    self.q_out = []
    for i in range(self.nb_threads):
      self.q_in.append(JoinableQueue(0))
      self.q_out.append(JoinableQueue(0))

  def get_batch_hbase(self):
    # legacy implementation: better to have a kafka topic for batches to be processed to allow
    #       safe parallelization on different machines
    # modified get_unprocessed_updates_from_date to get updates that were started but never finished
    try:
      # needs to read update table rows starting with 'index_update_'+extr and not marked as indexed.
      list_updates = self.indexer.get_unprocessed_updates_from_date(self.last_update_date_id, extr_type="_"+self.extr_prefix)
      if list_updates:
        for update_id, update_cols in list_updates:
          if self.extr_prefix in update_id:
            str_list_sha1s = update_cols[column_list_sha1s]
            list_sha1s = str_list_sha1s.split(',')
            print ("[{}.get_batch_hbase: log] Update {} has {} images.".format(self.pp, update_id, len(list_sha1s)))
            # also get 'ext:' to check if extraction was already processed?
            rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=[img_buffer_column, img_URL_column])
            #print "rows_batch", rows_batch
            if rows_batch:
              print("[{}.get_batch_hbase: log] Yielding for update: {}".format(self.pp, update_id))
              yield rows_batch, update_id
              print("[{}.get_batch_hbase: log] After yielding for update: {}".format(self.pp, update_id))
              self.last_update_date_id = '_'.join(update_id.split('_')[-2:])
            else:
              print("[{}.get_batch_hbase: log] Did not get any image buffers for the update: {}".format(self.pp, update_id))
          else:
            print("[{}.get_batch_hbase: log] Skipping update {} from another extraction type.".format(self.pp, update_id))
      else:
        print("[{}.get_batch_hbase: log] Nothing to update!".format(self.pp))
    except Exception as inst:
      full_trace_error("[{}.get_batch_hbase: error] {}".format(self.pp, inst))

  def is_udpate_unprocessd(self, update_id):
    update_rows = self.indexer.get_rows_by_batch([update_id], table_name=self.indexer.table_updateinfos_name)
    if update_rows:
      for row in update_rows:
        if "info:"+update_str_processed in row[1]:
          return False
    return True

  def get_batch_kafka(self):
    # Read from a kafka topic to allow safer parallelization on different machines
    try:
      # Needs to read topic to get update_id and list of sha1s
      for msg in self.ingester.consumer:
        msg_dict = json.loads(msg.value)
        update_id = msg_dict.keys()[0]
        # NB: Try to get update info and check it was really not processed yet.
        if self.is_udpate_unprocessd(update_id):
          str_list_sha1s = msg_dict[update_id]
          list_sha1s = str_list_sha1s.split(',')
          print("[{}.get_batch_kafka: log] Update {} has {} images.".format(self.pp, update_id, len(list_sha1s)))
          # NB: we could also get 'ext:' of images to double check if extraction was already processed
          #rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=["info:img_buffer"])
          rows_batch = self.indexer.get_columns_from_sha1_rows(list_sha1s, columns=[img_buffer_column, img_URL_column])
          #print "rows_batch", rows_batch
          if rows_batch:
            print("[{}.get_batch_kafka: log] Yielding for update: {}".format(self.pp, update_id))
            yield rows_batch, update_id
            print("[{}.get_batch_kafka: log] After yielding for update: {}".format(self.pp, update_id))
            self.last_update_date_id = '_'.join(update_id.split('_')[-2:])
          # Should we try to commit offset only at this point?
          else:
            print("[{}.get_batch_kafka: log] Did not get any image buffers for the update: {}".format(self.pp, update_id))
        else:
          print("[{}.get_batch_kafka: log] Skipping already processed update: {}".format(self.pp, update_id))
      else:
        print("[{}.get_batch_kafka: log] Nothing to update!".format(self.pp))
        # Fall back to checking HBase for unstarted/unfinished updates
        for rows_batch, update_id in self.get_batch_hbase():
          yield rows_batch, update_id
    except Exception as inst:
      full_trace_error("[{}.get_batch: error] {}".format(self.pp, inst))


  def process_batch(self):
    # Get a new update batch
    #for rows_batch, update_id in self.get_batch_hbase():
    for rows_batch, update_id in self.get_batch_kafka():
      try:
        start_update = time.time()
        print("[{}.process_batch: log] Processing update {} of {} rows.".format(self.pp, update_id, len(rows_batch)))
        sys.stdout.flush()

        # Initialize
        self.nb_empt = 0
        self.init_queues()
        threads = []

        # If we deleted an extractor at some point
        while len(self.extractors) < self.nb_threads:
          self.extractors.append(GenericExtractor(self.detector_type, self.featurizer_type, self.input_type,
                                                  self.extr_family_column, self.featurizer_prefix,
                                                  self.global_conf))


        # Mark batch as started to be process
        update_started_dict = {update_id: {'info:' + update_str_started: datetime.now().strftime('%Y-%m-%d:%H.%M.%S')}}
        self.indexer.push_dict_rows(dict_rows=update_started_dict, table_name=self.indexer.table_updateinfos_name)

        # Push images to queue
        list_in = []
        # For parallelized downloading...
        from Queue import Queue
        nb_imgs_dl = 0
        q_in_dl = Queue(0)
        q_out_dl = Queue(0)

        for img in rows_batch:
          # should decode base64
          if img_buffer_column in img[1]:
            tup = (img[0], img[1][img_buffer_column], False)
            list_in.append(tup)
          else:
            # need to re-download, accumulate a list of URLs to download
            if img_URL_column in img[1]:
              q_in_dl.put((img[0], img[1][img_URL_column], True))
              nb_imgs_dl += 1
            else:
              print("[{}.process_batch: warning] No buffer and no URL for image {} !".format(self.pp, img[0]))
              continue

        # Download missing images
        if nb_imgs_dl > 0:
          threads_dl = []
          for i in range(min(self.nb_threads, nb_imgs_dl)):
            # should read (url, obj_pos) from self.q_in
            # and push (url, obj_pos, buffer, img_info, start_process, end_process) to self.q_out
            thread = ThreadedDownloaderBufferOnly(q_in_dl, q_out_dl)
            thread.start()
            threads_dl.append(thread)

          q_in_dl.join()

          # Push downloaded images to list_in too
          nb_dl = 0
          while nb_dl < nb_imgs_dl:
            sha1, buffer, push_back, inst = q_out_dl.get()
            nb_dl += 1
            if inst:
              if self.verbose > 0:
                print("[{}.process_batch: log] Could not download image {}, error was: {}".format(self.pp, sha1, inst))
            else:
              if buffer:
                list_in.append((sha1, buffer, push_back))
              else:
                # Is that possible?
                print("[{}.process_batch: error] No error but no buffer either for image {}".format(self.pp, sha1))


        buff_msg = "[{}.process_batch: log] Got {}/{} image buffers for update {}."
        print(buff_msg.format(self.pp, len(list_in), len(rows_batch), update_id))
        sys.stdout.flush()

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
        thread_creation_failed = [0]*self.nb_threads
        for i in range(self.nb_threads):
          # one per non empty input queue
          if q_in_size[i] > 0:
            try:
              thread = DaemonBatchExtractor(self.extractors[i], self.q_in[i], self.q_out[i], verbose=self.verbose)
              # Could get a 'Cannot allocate memory' if we are using too many threads...
              thread.start()
              threads.append(thread)
            except OSError as inst:
              # Should we try to push self.q_in[i] data to some other thread?
              print("[{}.process_batch: error] Could not start thread #{}: {}".format(self.pp, i+1, inst))
              thread_creation_failed[i] = 1
              time.sleep(10*sum(thread_creation_failed))

        if sum(thread_creation_failed) == self.nb_threads:
          raise ValueError("Could not start any thread...")

        nb_threads_running = len(threads)
        start_process = time.time()
        stop = time.time() + self.max_proc_time
        # Wait for all tasks to be marked as done
        threads_finished = [0] * nb_threads_running
        deleted_extr = [0] * nb_threads_running
        while sum(threads_finished) < nb_threads_running:
          for i in range(nb_threads_running):
            if sum(threads_finished) == nb_threads_running:
              sys.stdout.flush()
              break
            if threads_finished[i] == 1:
              continue
            i_q_in = i + sum(thread_creation_failed[:i + 1])
            if q_in_size[i_q_in] > 0:
              # This seems to block forever sometimes, if subprocess crashed?...
              #self.q_in[i].join()
              # Manual join with timeout...
              # https://github.com/python/cpython/blob/3.6/Lib/multiprocessing/queues.py
              if not self.q_in[i_q_in]._unfinished_tasks._semlock._is_zero() and time.time() < stop:
                time.sleep(1)
              else:
                if self.q_in[i_q_in]._unfinished_tasks._semlock._is_zero() and self.verbose > 2:
                  end_msg = "Thread {}/{} (pid: {}) marked as finished because processing seems finished"
                  print(end_msg.format(i+1, nb_threads_running, threads[i].pid))
                else:
                  if self.verbose > 0:
                    timeout_msg = "Thread {}/{} (pid: {}) force marked task as done because max_proc_time ({}) has passed."
                    self.q_in[i_q_in].task_done()
                    print(timeout_msg.format(i+1, nb_threads_running, threads[i].pid, self.max_proc_time))
                    sys.stdout.flush()
                    # Try to delete corresponding extractor to free memory?
                    # And reduce number of threads at the end of the loop
                    if deleted_extr[i] == 0:
                      # since we pushed the extractor as self.extractors[i] in a loop of self.nb_threads we use i_q_in
                      del self.extractors[i_q_in]
                      deleted_extr[i] = 1
                threads_finished[i] = 1
            else:
              if self.verbose > 2:
                # We actually never gave something to process...
                noproc_msg = "Thread {}/{} (pid: {}) marked as finished because no data was passed to it"
                print(noproc_msg.format(i+1, nb_threads_running, threads[i].pid))
              threads_finished[i] = 1

        # Cleanup threads to free memory before getting data back
        del threads

        # Gather results
        q_out_size = []
        q_out_size_tot = 0
        for i in range(self.nb_threads):
          q_out_size.append(self.q_out[i].qsize())
          q_out_size_tot += q_out_size[i]

        if self.verbose > 3:
          print("[{}.process_batch: log] Total output queues size is: {}".format(self.pp, q_out_size_tot))
          sys.stdout.flush()

        # Can get stuck here?
        dict_imgs = dict()
        for i in range(self.nb_threads):
          if self.verbose > 4:
            print("Thread {} q_out_size: {}".format(i+1, q_out_size[i]))
            sys.stdout.flush()
          while q_out_size[i]>0 and not self.q_out[i].empty():
            if self.verbose > 4:
              print("Thread {} q_out is not empty.".format(i + 1))
              sys.stdout.flush()
            try:
              batch_out = self.q_out[i].get(True, 10)
              if self.verbose > 2:
                print("Got batch of {} features from thread {} q_out.".format(len(batch_out), i + 1))
                sys.stdout.flush()
              for sha1, dict_out in batch_out:
                dict_imgs[sha1] = dict_out
            except:
              if self.verbose > 1:
                print("Thread {} failed to get from q_out: {}".format(i+1))
                sys.stdout.flush()
              pass
            if self.verbose > 3:
              print("Marking task done in q_out of thread {}.".format(i + 1))
              sys.stdout.flush()
            self.q_out[i].task_done()

        if self.verbose > 0:
          print_msg = "[{}.process_batch: log] Got features for {}/{} images in {}s."
          print(print_msg.format(self.pp, len(dict_imgs.keys()), len(list_in), time.time() - start_process))
          sys.stdout.flush()

        # Push them
        self.indexer.push_dict_rows(dict_rows=dict_imgs, table_name=self.indexer.table_sha1infos_name)

        # Mark batch as processed
        update_processed_dict = {update_id: {'info:' + update_str_processed: datetime.now().strftime('%Y-%m-%d:%H.%M.%S')}}
        self.indexer.push_dict_rows(dict_rows=update_processed_dict, table_name=self.indexer.table_updateinfos_name)

        # Cleanup
        del self.q_in
        del self.q_out

        # To try to adjust a too optimistic nb_threads setting
        # if (sum(thread_creation_failed) > 0 or sum(deleted_extr) > 0) and self.nb_threads > 2:
        #   self.nb_threads -= 1

        print_msg = "[{}.process_batch: log] Completed update {} in {}s."
        print(print_msg.format(self.pp, update_id, time.time() - start_update))
        sys.stdout.flush()
        self.nb_err = 0

        # Force garbage collection?
        gc.collect()

        # Should we just raise an Exception and restart clean?
        if sum(thread_creation_failed) > 0 or sum(deleted_extr) > 0:
           raise ValueError("Something went wrong. Trying to restart clean")

      except Exception as inst:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fulltb = traceback.format_tb(exc_tb)
        raise type(inst)(" {} ({})".format(inst, ''.join(fulltb)))

  def run(self):
    self.nb_empt = 0
    self.nb_err = 0
    while True:
      self.process_batch()
      print("[ExtractionProcessor: log] Nothing to process at: {}".format(datetime.now().strftime('%Y-%m-%d:%H.%M.%S')))
      sys.stdout.flush()
      time.sleep(10*self.nb_empt)
      self.nb_empt += 1

      # try:
      #   self.process_batch()
      #   print("[ExtractionProcessor: log] Nothing to process at: {}".format(datetime.now().strftime('%Y-%m-%d:%H.%M.%S')))
      #   sys.stdout.flush()
      #   time.sleep(10*self.nb_empt)
      #   self.nb_empt += 1
      # except Exception as inst:
      #   err_msg = "ExtractionProcessor died: {} {}".format(type(inst), inst)
      #   full_trace_error(err_msg)
      #   sys.stdout.flush()
      #   time.sleep(10 * self.nb_err)
      #   self.nb_err += 1


if __name__ == "__main__":

  # Get conf file
  parser = ArgumentParser()
  parser.add_argument("-c", "--conf", dest="conf_file", required=True)
  parser.add_argument("-p", "--prefix", dest="prefix", default=default_extr_proc_prefix)
  options = parser.parse_args()

  # TODO: should we daemonize that too?

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
      del ep
      gc.collect()
      time.sleep(10*nb_err)
      ep = ExtractionProcessor(options.conf_file, prefix=options.prefix)
      nb_err += 1