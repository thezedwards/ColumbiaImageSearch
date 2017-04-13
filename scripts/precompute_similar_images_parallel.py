import os
import sys
import time
import datetime
import happybase

# parallel
from multiprocessing import JoinableQueue as Queue
from multiprocessing import Process

sys.path.append('..')
import cu_image_search
from cu_image_search.search import searcher_hbaseremote

nb_workers = 16
time_sleep = 60

def producer(global_conf_file, queueIn, queueProducer):
    print "[producer: log] Started a producer worker (pid: {}) at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    searcher_producer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[producer: log] Producer worker ready at {}".format(get_now())
    queueProducer.put("Producer ready")
    while True:
        start_get_batch = time.time()
        update_id, str_list_sha1s = searcher_producer.indexer.get_next_batch_precomp_sim()
        #queueProducer.put("Producer got batch")
        print "[producer: log] Got batch in {}s at {}".format(time.time() - start_get_batch, get_now())
        sys.stdout.flush()
        if update_id is None:
            print "[producer: log] No more update to process."
        else:
            start_precomp = time.time()
            # check that sha1s of batch have no precomputed similarities already in sha1_infos table
            valid_sha1s, not_indexed_sha1s, precomp_sim_sha1s = check_indexed_noprecomp(searcher_producer, str_list_sha1s.split(','))
        
            searcher_producer.indexer.write_batch([(update_id, {searcher_producer.indexer.precomp_start_marker: 'True'})], searcher_producer.indexer.table_updateinfos_name)
            # push updates to be processed in queueIn
            # https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Queue.qsize
            # qsize raises NotImplemented Error on OS X...
            #print "[producer: log] Pushing update {} in queue containing {} items at {}.".format(update_id, queueIn.qsize(), get_now())
            print "[producer: log] Pushing update {} at {}.".format(update_id, get_now())
            sys.stdout.flush()
            queueIn.put((update_id, valid_sha1s, start_precomp))


def consumer(global_conf_file, queueIn, queueOut, queueConsumer):
    print "[consumer: log] Started a consumer worker (pid: {}) at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    searcher_consumer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[consumer: log] Consumer worker (pid: {}) ready at {}".format(os.getpid(), get_now())
    queueConsumer.put("Consumer ready")
    sys.stdout.flush()
    while True:
        ## reads from queueIn
        print "[consumer: log] Consumer worker (pid: {}) waiting for update at {}".format(os.getpid(), get_now())
        sys.stdout.flush()
        update_id, valid_sha1s, start_precomp = queueIn.get()
        ## search
        print "[consumer: log] Consumer worker (pid: {}) computing similarities for {} valid sha1s of update {} at {}".format(os.getpid(), len(valid_sha1s), update_id, get_now())
        sys.stdout.flush()
        start_search = time.time()
        # precompute similarities using searcher 
        # for v1 check_indexed_noprecomp
        #simname, corrupted = searcher_consumer.search_from_sha1_list_get_simname(valid_sha1s, update_id)
        simname, corrupted = searcher_consumer.search_from_listid_get_simname(valid_sha1s, update_id)
        elapsed_search = time.time() - start_search
        print "[consumer: log] Processed update {} at {}. Search performed in {}s.".format(update_id, get_now(), elapsed_search)
        sys.stdout.flush()
        ## push to queueOut
        queueOut.put((update_id, simname, valid_sha1s, corrupted, start_precomp, elapsed_search))
        queueIn.task_done()


def finalizer(global_conf_file, queueOut, queueFinalizer):
    print "[finalizer: log] Started a finalizer worker (pid: {}) at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    searcher_finalizer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[finalizer: log] Finalizer worker (pid: {}) ready at {}".format(os.getpid(), get_now())
    queueFinalizer.put("Finalizer ready")
    while True:
        ## Read from queueOut
        update_id, simname, valid_sha1s, corrupted, start_precomp, elapsed_search = queueOut.get()
        print "[finalizer: log] Finalizer worker (pid: {}) got update {} to finalize at {}".format(os.getpid(), update_id, get_now())
        sys.stdout.flush()
        ## Push computed similarities
        # format for saving in HBase:
        # - batch_sim: should be a list of sha1 row key, dict of "s:similar_sha1": dist_value
        # - batch_mark_precomp_sim: should be a list of sha1 row key, dict of precomp_sim_column: True
        batch_sim, batch_mark_precomp_sim = format_batch_sim(simname, valid_sha1s, corrupted, searcher_finalizer)

        # push similarities to HBI_table_sim (escorts_images_similar_row_dev) using searcher.indexer.write_batch
        searcher_finalizer.indexer.write_batch(batch_sim, searcher_finalizer.indexer.table_sim_name)
        # push to weekly update table for Amandeep to integrate in DIG
        week, year = get_week_year()
        weekly_sim_table_name = searcher_finalizer.indexer.table_sim_name+"_Y{}W{}".format(year, week)
        print "[finalizer: log] weekly table name: {}".format(weekly_sim_table_name)
        weekly_sim_table = searcher_finalizer.indexer.get_create_table(weekly_sim_table_name, families={'s': dict()})
        searcher_finalizer.indexer.write_batch(batch_sim, weekly_sim_table_name)

        ## Mark as done
        # mark precomp_sim true in escorts_images_sha1_infos_dev
        searcher_finalizer.indexer.write_batch(batch_mark_precomp_sim, searcher_finalizer.indexer.table_sha1infos_name)
        # mark info:precomp_finish in escorts_images_updates_dev
        if not corrupted: # do not mark finished if we faced some issue? mark as corrupted?
            searcher_finalizer.indexer.write_batch([(update_id, {searcher_finalizer.indexer.precomp_end_marker: 'True'})],
                                                   searcher_finalizer.indexer.table_updateinfos_name)
        print("[finalizer: log] Finalize update {} at {} in {}s total.".format(update_id, get_now(), time.time() - start_precomp))
        sys.stdout.flush()
        ## Cleanup
        if simname:
            # remove simname 
            os.remove(simname)
            # remove features file
            featfirst = simname.split('-')[0]
            featfn = featfirst+'.dat'
            #print "[process_one_update: log] Removing file {}".format(featfn)
            os.remove(featfn)
        queueOut.task_done()


def get_now():
    return datetime.datetime.now().strftime("%Y-%m-%d:%H.%M.%S")


def get_week(today=datetime.datetime.now()):
    return today.strftime("%W")


def get_year(today=datetime.datetime.now()):
    return today.strftime("%Y")


def get_week_year(today=datetime.datetime.now()):
    week = get_week(today)
    year = get_year(today)
    return week, year


def check_indexed_noprecomp(searcher, list_sha1s):
    print "[check_indexed_noprecomp: log] verifying validity of list_sha1s."
    sys.stdout.flush()
    columns_check = [searcher.indexer.cu_feat_id_column, searcher.indexer.precomp_sim_column]
    # Is this blocking in parallel mode?
    rows = searcher.indexer.get_columns_from_sha1_rows(list_sha1s, columns=columns_check)
    not_indexed_sha1s = []
    precomp_sim_sha1s = []
    valid_sha1s = []
    for row in rows:
        #print row
        # check searcher.indexer.cu_feat_id_column exists
        if searcher.indexer.cu_feat_id_column not in row[1]:
            not_indexed_sha1s.append(str(row[0]))
            print "[check_indexed_noprecomp: log] found unindexed image {}".format(str(row[0]))
            sys.stdout.flush()
            continue
        # check searcher.indexer.precomp_sim_column does not exist
        if searcher.indexer.precomp_sim_column in row[1]:
            precomp_sim_sha1s.append(str(row[0]))
            print "[check_indexed_noprecomp: log] found image {} with already precomputed similar images".format(str(row[0]))
            sys.stdout.flush()
            continue
        valid_sha1s.append((int(row[1][searcher.indexer.cu_feat_id_column]), str(row[0])))
    # v1 was:
    #valid_sha1s = list(set(list_sha1s) - set(not_indexed_sha1s) - set(precomp_sim_sha1s))
    msg = "{} valid sha1s, {} not indexed sha1s, {} already precomputed similarities sha1s."
    print("[check_indexed_noprecomp: log] "+msg.format(len(valid_sha1s), len(not_indexed_sha1s), len(precomp_sim_sha1s)))
    sys.stdout.flush()
    return valid_sha1s, not_indexed_sha1s, precomp_sim_sha1s


def read_sim_precomp(simname, nb_query, searcher):
    # intialization
    sim = []
    sim_score = []
    if simname is not None:
        # read similar images
        count = 0
        f = open(simname);
        for line in f:
            #sim_index.append([])
            nums = line.replace(' \n','').split(' ')
            #filter near duplicate here
            nums = searcher.filter_near_dup(nums, searcher.near_dup_th)
            #print nums
            onum = len(nums)/2
            n = onum
            #print n
            if onum==0: # no returned images, e.g. no near duplicate
                sim.append(())
                sim_score.append([])
                continue
            # get the sha1s of similar images
            sim_infos = [searcher.indexer.sha1_featid_mapping[int(i)] for i in nums[0:n]]
            # beware, need to make sure sim and sim_score are still aligned
            #print("[read_sim] got {} sim_infos from {} samples".format(len(sim_infos), n))
            sim.append(sim_infos)
            sim_score.append(nums[onum:onum+n])
            count = count + 1
            if count == nb_query:
                break
        f.close()
    return sim, sim_score
    

def format_batch_sim(simname, valid_sha1s, corrupted, searcher):
    # format similarities for HBase output
    nb_query = len(valid_sha1s) - len(corrupted)
    sim, sim_score = read_sim_precomp(simname, nb_query, searcher)
    # batch_sim: should be a list of sha1 row key, dict of all "s:similar_sha1": dist_value
    batch_sim = []
    # batch_mark_precomp_sim: should be a list of sha1 row key, dict of precomp_sim_column: True
    batch_mark_precomp_sim = []
    if sim:
        if len(sim) != len(valid_sha1s) or len(sim_score) != len(valid_sha1s):
            print "[format_batch_sim: warning] similarities and queries count are different."
            print "[format_batch_sim: warning] corrupted is: {}.".format(corrupted)
        # deal with corrupted
        i_img = 0
        for i,sha1 in enumerate(valid_sha1s):
            if sha1 in corrupted:
                continue
            sim_columns = dict()
            for i_sim,sim_img in enumerate(sim[i_img]):
                sim_columns["s:"+str(sim_img)] = str(sim_score[i_img][i_sim])
            sim_row = (sha1, sim_columns)
            batch_sim.append(sim_row)
            batch_mark_precomp_sim.append((sha1,{searcher.indexer.precomp_sim_column: 'True'}))
            i_img += 1
    #print batch_sim
    #print batch_mark_precomp_sim
    return batch_sim, batch_mark_precomp_sim


def parallel_precompute(global_conf_file):
    # Define queues
    queueIn = Queue(nb_workers*2)
    queueOut = Queue(nb_workers*2)
    queueProducer = Queue()
    queueFinalizer = Queue()
    queueConsumer = Queue(nb_workers)

    # Start finalizer
    t = Process(target=finalizer, args=(global_conf_file, queueOut, queueFinalizer))
    t.daemon = True
    t.start()
    # Start consumers
    for i in range(nb_workers):
        t = Process(target=consumer, args=(global_conf_file, queueIn, queueOut, queueConsumer))
        t.daemon = True
        t.start()
    # Start producer
    t = Process(target=producer, args=(global_conf_file, queueIn, queueProducer))
    t.daemon = True
    t.start()

    # Wait for everything to be started properly
    producerOK = queueProducer.get()
    finalizerOK = queueFinalizer.get()
    for i in range(nb_workers):
        consumerOK = queueConsumer.get()
    print "[parallel_precompute: log] All workers are ready."
    sys.stdout.flush()
    # Wait for everything to be finished
    time.sleep(time_sleep)
    queueIn.join()
    queueOut.join()


if __name__ == "__main__":
    
    """ Run precompute similar images based on `conf_file` given as parameter
    """
    if len(sys.argv)<2:
        print "python precompute_similar_images_parallel.py conf_file"
        exit(-1)
    global_conf_file = sys.argv[1]
    
    while True:
        parallel_precompute(global_conf_file)
        print "[precompute_similar_images_parallel: log] Nothing to compute. Sleeping for {}s.".format(time_sleep)
        time.sleep(time_sleep)
    
    
    