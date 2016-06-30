import os
import sys
import time
import MySQLdb
from generic_indexer import GenericIndexer
from ..image_downloader.file_downloader import FileDownloader
from ..memex_tools.sha1_tools import get_SHA1_from_file, get_SHA1_from_data

class LocalIndexer(GenericIndexer):
        
    def initialize_indexer_backend(self):
        """ Initialize backend.
        """
        self.image_downloader = None
        print "[LocalIndexer: log] initialized with values {},{},{},{}.".format(self.local_db_host,self.local_db_user,self.local_db_pwd,self.local_dbname)
        if self.image_downloader_type=="file_downloader":
            self.image_downloader = FileDownloader(self.global_conf_file)
        else:
            raise ValueError("[LocalIndexer.initialize_indexer_backend error] Unsupported image_downloader_type: {}.".format(self.image_downloader_type))
        # initialize db config
        self.read_db_conf()
        # intialize hasher and feature_extractor too

    def read_db_conf(self):
        self.local_db_host = self.global_conf['LI_local_db_host']
        self.local_db_user = self.global_conf['LI_local_db_user']
        self.local_db_pwd = self.global_conf['LI_local_db_pwd']
        self.local_dbname = self.global_conf['LI_local_dbname']
        self.master_update_filepath = self.global_conf['LI_master_update_filepath']

    def open_localdb_connection(self):
        self.db = MySQLdb.connect(host=self.local_db_host,user=self.local_db_user,passwd=self.local_db_pwd,db=self.local_dbname)

    def close_localdb_connection(self):
        self.db.close()

    def get_next_batch_start(self):
        """ Get start value for next update batch
        :returns htid: Biggest htid in local database.
        """
        return self.get_max_ht_id()

    def is_indexed(self,sha1):
        # query index with single SHA1
        pass

    def are_indexed(self,sha1_list):
        # query index with list of SHA1
        pass

    def get_max_unique_id(self):
        """ Get max `id` from `uniqueIds` table in local MySQL.
        """
        self.open_localdb_connection()
        c = self.db.cursor()
        c.execute('select id from uniqueIds ORDER BY id DESC limit 1;')
        remax = c.fetchall()
        if len(remax):
            umax = int(remax[0][0])
        else:
            umax = 0
        self.close_localdb_connection()
        return umax

    def get_max_ht_id(self):
        """ Get max `htid` from `fullIds` table in local MySQL.
        """
        self.open_localdb_connection()
        c = self.db.cursor()
        c.execute('select htid from fullIds ORDER BY id DESC limit 1;')
        remax = c.fetchall()
        if len(remax):
            fmax = int(remax[0][0])
        else:
            fmax = 0
        self.close_localdb_connection()
        return fmax

    def get_max_full_id(self):
        """ Get max `id` from `fullIds` table in local MySQL.
        """
        self.open_localdb_connection()
        c = self.db.cursor()
        c.execute('select id from fullIds ORDER BY id DESC limit 1;')
        remax = c.fetchall()
        if len(remax):
            fmax = int(remax[0][0])
        else:
            fmax = 0
        self.close_localdb_connection()
        return fmax

    def get_old_unique_ids(self,unique_sha1):
        self.open_localdb_connection()
        c = self.db.cursor()
        sql='SELECT htid,sha1 FROM uniqueIds WHERE sha1 in (%s);' 
        in_p=', '.join(map(lambda x: '%s', unique_sha1))
        sqlq = sql % (in_p)
        c.execute(sqlq, unique_sha1)
        re = c.fetchall()
        self.close_localdb_connection()
        old_uniques = [i[1] for i in re]
        old_uniques_htid = [int(i[0]) for i in re]
        return old_uniques,old_uniques_htid

    def get_new_unique_images(self,sha1_images):
        # get unique images 
        sha1_list = [img_item[-1] for img_item in sha1_images]
        unique_sha1 = sorted(set(sha1_list))
        unique_idx = [sha1_list.index(sha1) for sha1 in unique_sha1]
        full_idx = [unique_sha1.index(sha1) for sha1 in sha1_list]
        
        old_uniques,old_uniques_htid = self.get_old_unique_ids(unique_sha1)
        
        new_uniques=[]
        unique_htid = []
        new_files=[]
        for i in range(0,len(unique_sha1)):
            if unique_sha1[i] not in old_uniques:
                img_item = sha1_images[unique_idx[i]]
                new_uniques.append((int(img_item[0]),img_item[1],img_item[-1]))
                new_files.append(img_item[-2])
                unique_htid.append(new_uniques[-1][0])
            else:
                unique_htid.append(old_uniques_htid[old_uniques.index(unique_sha1[i])])
        new_fulls = []
        for i in range(0,len(sha1_images)):
            new_fulls.append((int(sha1_images[i][0]),unique_htid[full_idx[i]]))
        return new_files,new_uniques,new_fulls

    def insert_new_uniques(self,new_uniques):
        """ Insert new_uniques ids in the local database.

        :param new_uniques: list of tuples (htid, location, sha1) to be inserted.
        :type new_uniques: list
        """
        self.open_localdb_connection()
        c = self.db.cursor()
        if num_new_unique:
            insert_statement = "INSERT IGNORE INTO uniqueIds (htid, location, sha1) VALUES {}".format(','.join(map(str,new_uniques)))
            c.execute(insert_statement)
        self.close_localdb_connection()

    def insert_new_fulls(self,new_fulls):
        """ Insert new_fulls ids in the local database.

        :param new_fulls: list of tuples (htid, uid) to be inserted.
        :type new_fulls: list
        """
        insert_statement = "INSERT IGNORE INTO fullIds (htid, uid) VALUES {}".format(','.join(map(str,new_fulls)))
        c.execute(insert_statement)
        self.close_localdb_connection()

    def check_batch(self,umax,umax_new,num_new_unique,fmax_new,fmax,num_readable,hashbits_filepath,feature_filepath):
        if umax_new-umax != num_new_unique:
            print 'Update failed! unique table size mismatch!',umax_new,umax,num_new_unique
        elif os.stat(hashbits_filepath).st_size!=num_new_unique*32:
            print 'Update failed! hash bits size mismatch!',os.stat(hashbits_filepath).st_size,num_new_unique*32
        elif os.stat(feature_filepath).st_size!=num_new_unique*16384:
            print 'Update failed! feature size mismatch!',os.stat(feature_filepath).st_size,num_new_unique*16384
        elif fmax_new-fmax != num_readable:
            print 'Update warning! full table size mismatch!',fmax_new,fmax,num_readable
        else:
            return True
        return False

    def update_master_file(self,startid):
        with open(self.master_update_filepath, "a") as f:
                f.write(startid+'\n')

    def finalize_update(success,hashbits_filepath,feature_filepath,startid):
        if not success:
            if os.path.isfile(hashbits_filepath):
                os.remove(hashbits_filepath)
            if os.path.isfile(feature_filepath):
                os.remove(feature_filepath)
        else:
            self.update_master_file(startid)
            self.hasher.compress_feats()
            #vmtouch hashing and features folder
            os.system('./cache.sh')

            ## delete img cache # should be done in search.
            #os.system('find img -name "*sim_*.txt" -exec rm -rf {} \;')
            #os.system('find img -name "*sim_*.json" -exec rm -rf {} \;')
            

    def index_batch(self,batch):
        """ Index a batch in the form of a list of (id,url,other_data)
        """
        # Download images
        startid = batch[0][0]
        readable_images = self.image_downloader.download_images(batch,startid)
        #print readable_images
        # Compute sha1
        sha1_images = [img+(get_SHA1_from_file(img[-1]),) for img in readable_images]
        print "[LocalIndexer.index_batch: log] sha1_images",sha1_images
        # Record current biggest ids
        umax = self.get_max_unique_id()
        fmax = self.get_max_full_id()
        # Find new images
        new_files,new_uniques,new_fulls = self.get_new_unique_images(sha1_images)
        # Compute features
        features_filename,ins_num = self.feature_extractor.compute_features(new_files,startid)
        # Compute hashcodes
        hashbits_filepath = self.hasher.compute_hashcodes(features_filename,ins_num,startid)
        # Insert new ids
        self.insert_new_uniques(new_uniques)
        self.insert_new_fulls(new_fulls)
        # Check that batch processing went well
        umax_new = self.get_max_unique_id()
        fmax_new = self.get_max_full_id()
        update_success = self.check_batch(umax,umax_new,len(new_uniques),fmax_new,fmax,len(sha1_images),hashbits_filepath,feature_filepath)
        if update_success:
            print "Update succesful!"
            self.open_localdb_connection()
            self.db.commit()
            self.close_localdb_connection()
        self.finalize(update)

        
