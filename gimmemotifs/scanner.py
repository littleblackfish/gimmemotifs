import os
import re
import sys
from functools import partial
from tempfile import mkdtemp,NamedTemporaryFile

# "hidden" features, in development
try:
    import MOODS.tools
    import MOODS.parsers
    import MOODS.scan
except ImportError:
    pass

import numpy as np

from gimmemotifs.background import RandomGenomicFasta
from gimmemotifs.config import MotifConfig,CACHE_DIR
from gimmemotifs.fasta import Fasta
from gimmemotifs.genome_index import GenomeIndex
from gimmemotifs.c_metrics import pwmscan
from gimmemotifs.motif import read_motifs
from gimmemotifs.utils import parse_cutoff,get_seqs_type,file_checksum
from gimmemotifs.genome_index import rc,check_genome

from diskcache import Cache
from scipy.stats import scoreatpercentile
import numpy as np

# only used when using cache, should not be a requirement
try:
    from dogpile.cache import make_region
    from dogpile.cache.api import NO_VALUE
    from cityhash import CityHash64
except ImportError:
    pass 

config = MotifConfig()

def scan_fasta_to_best_score(fname, motifs):
    """Scan a FASTA file with motifs.

    Scan a FASTA file and return a dictionary with the best score per motif.

    Parameters
    ----------
    fname : str
        Filename of a sequence file in FASTA format.

    motifs : list
        List of motif instances.

    Returns
    -------
    result : dict
        Dictionary with motif scanning results.
    """
    # Initialize scanner
    s = Scanner()
    s.set_motifs(motifs)

    sys.stderr.write("scanning {}...\n".format(fname))
    result = dict([(m.id, []) for m in motifs])
    for scores in s.best_score(fname):
        for motif,score in zip(motifs, scores):
            result[motif.id].append(score)

    return result

def parse_threshold_values(motif_file, cutoff):
    motifs = read_motifs(open(motif_file))
    d = parse_cutoff(motifs, cutoff)
    threshold = {}
    for m in motifs:
        c = m.pwm_min_score() + ( 
                    m.pwm_max_score() - m.pwm_min_score()
                ) * d[m.id]
        threshold[m.id] = c
    return threshold

def scan_sequence(seq, motifs, nreport, scan_rc):
    
    ret = []
    # scan for motifs
    for motif, cutoff in motifs:
        if cutoff is None:
            ret.append([])
        else:
            result = pwmscan(seq, motif.pwm, cutoff, nreport, scan_rc)
            if cutoff <= motif.pwm_min_score() and len(result) == 0:
                result = [[motif.pwm_min_score(), 0, 1]] * nreport
            ret.append(result)

    # return results
    return ret

def scan_region(region, genome_index, motifs, nreport, scan_rc):
    
    # retrieve sequence
    chrom,start,end = re.split(r'[:-]', region)
    seq = genome_index.get_sequence(chrom, int(start), int(end)).upper()
    
    return scan_sequence(seq, motifs, nreport, scan_rc)

def scan_seq_mult(seqs, motifs, nreport, scan_rc):
    ret = []
    for seq in seqs:
        result = scan_sequence(seq.upper(), motifs, nreport, scan_rc)
        ret.append(result)
    return ret

def scan_region_mult(regions, genome_index, motifs, nreport, scan_rc):
    ret = []
    for region in regions:
        result = scan_region(region, genome_index, motifs, nreport, scan_rc)
        ret.append(result)
    return ret


def scan_fa_with_motif_moods(fo, motifs, matrices, bg, thresholds, nreport, scan_rc=True):

    scanner = MOODS.scan.Scanner(7)
    scanner.set_motifs(matrices, bg, thresholds)

    ret = []
    for name, seq in fo.items():
        l = len(seq)

        scan_seq = seq.upper()
        if scan_rc:
            scan_seq = "".join((scan_seq, "N"*50, rc(scan_seq)))
        results = scanner.scan_max_hits(scan_seq, nreport)
        for motif,result in zip(motifs, results):
            matches = []
            for match in result:
                strand = 1
                pos = match.pos
                if scan_rc:
                    if pos > l:
                        pos = l - (pos - l - 50) - len(motif)
                        strand = -1
                matches.append((pos, match.score, strand))
            ret.append((motif, {name: matches}))

    return ret

def scan_fa_with_motif_moods_count(fo, motifs, matrices, bg, thresholds, nreport, scan_rc=True):
    scanner = MOODS.scan.Scanner(7)
    scanner.set_motifs(matrices, bg, thresholds)

    ret = []
    for name, seq in fo.items():
        l = len(seq)

        scan_seq = seq.upper()
        if scan_rc:
            scan_seq = "".join((scan_seq, "N"*50, rc(scan_seq)))
        results = scanner.counts_max_hits(scan_seq, nreport)
        ret.append((name, results))

    return ret

def calc_threshold_moods(m, c):
    m_min = MOODS.tools.min_score(m)
    m_max = MOODS.tools.max_score(m)

    return m_min + (m_max - m_min) * c

def scan_it_moods(infile, motifs, cutoff, bgfile, nreport=1, scan_rc=True, pvalue=None, count=False):
    tmpdir = mkdtemp()
    matrices = []
    pseudocount = 1e-3
    #sys.stderr.write("bgfile: {}\n".format(bgfile))
    bg = MOODS.tools.bg_from_sequence_dna("".join(Fasta(bgfile).seqs), 1)

    for motif in motifs:
        pfmname = os.path.join(tmpdir, "{}.pfm".format(motif.id))
        with open(pfmname, "w") as f:
            matrix = np.array(motif.pwm).transpose()
            for line in [" ".join([str(x) for x in row]) for row in matrix]:
                f.write("{}\n".format(line))

        matrices.append(MOODS.parsers.pfm_log_odds(pfmname, bg, pseudocount))

    thresholds = []
    if pvalue is not None:
        thresholds = [MOODS.tools.threshold_from_p(m, bg, float(pvalue)) for m in matrices]
        #sys.stderr.write("{}\n".format(thresholds))
    else:
        thresholds = [calc_threshold_moods(m, float(cutoff)) for m in matrices]

    scanner = MOODS.scan.Scanner(7)
    scanner.set_motifs(matrices, bg, thresholds)

    config = MotifConfig()
    ncpus =  int(config.get_default_params()['ncpus'])
    fa = Fasta(infile)
    chunk = 500
    if (len(fa) / chunk) < ncpus:
        chunk = len(fa) / (ncpus + 1)

    jobs = []
    func = scan_fa_with_motif_moods
    if count:
        func = scan_fa_with_motif_moods_count

    for i in range(0, len(fa), chunk):
        jobs.append(pool.apply_async(
                                          func,
                                          (fa[i:i + chunk],
                                          motifs,
                                          matrices,
                                          bg,
                                          thresholds,
                                          nreport,
                                          scan_rc,
                                          )))

    for job in jobs:
        for ret in job.get():
            yield ret

class Scanner(object):
    """
    scan sequences with motifs
    """
    
    def __init__(self):
        self.config = MotifConfig()
        self.threshold = None

        self.use_cache = False
        if self.config.get_default_params().get("use_cache", False):
            self._init_cache()
            
    def _init_cache(self):
        try:
            self.cache = make_region().configure(
                'dogpile.cache.pylibmc',
                expiration_time = 3600,
                arguments = {
                    'url':["127.0.0.1"],
                    'binary': True,
                    }
                #    'dogpile.cache.dbm',
                #    expiration_time = 3600,
                #    arguments = {
                #        'filename': 'cache.dbm'
                #    }
            )
            self.use_cache = True
        except Exception as e:
            sys.stderr.write("failed to initialize cache\n")
            sys.stderr.write("{}\n".format(e))

    def set_motifs(self, motifs):
        try:
            # Check if motifs is a list of Motif instances
            motifs[0].to_pwm()
            tmp = NamedTemporaryFile(delete=False)
            for m in motifs:
                tmp.write("{}\n".format(m.to_pwm()))
            tmp.close()
            motif_file = tmp.name
        except AttributeError as e:
            motif_file = motifs

        self.motifs = motif_file
        self.motif_ids = [m.id for m in read_motifs(open(motif_file))]
        self.checksum = {}
        if self.use_cache:
            chksum = CityHash64("\n".join(sorted(self.motif_ids)))
            self.checksum[self.motif_file] = chksum

    def _threshold_from_seqs(self, motifs, seqs, fdr):
        scan_motifs = [(m, m.pwm_min_score()) for m in motifs]
        
        table = []
        for x in self._scan_sequences_with_motif(scan_motifs, seqs, 1, True):
            table.append([row[0][0] for row in x])
                
        for (motif, _), scores in zip(scan_motifs, np.array(table).transpose()):
            min_score = motif.pwm_min_score()
            cutoff = 0
            if len(scores) > 0:
                opt_score = scoreatpercentile(scores, 100 - (100 * fdr))
                cutoff = (opt_score - min_score) / (motif.pwm_max_score() - min_score)
            yield motif, opt_score#cutoff


    def set_threshold(self, fdr=None, threshold=None, genome=None, 
                        length=200, filename=None):
        """Set motif scanning threshold based on background sequences.

        Parameters
        ----------
        fdr : float, optional
            Desired FDR, between 0.0 and 1.0.

        threshold : float or str, optional
            Desired motif threshold, expressed as the fraction of the 
            difference between minimum and maximum score of the PWM.
            Should either be a float between 0.0 and 1.0 or a filename
            with thresholds as created by 'gimme threshold'.

        """
        if threshold:
            if fdr:
                raise ValueError("Need either fdr or threshold.")
            if genome:
                sys.stderr.write(
                    "Parameter genome ignored when threshold is specified.\n"
                    "Did you want to use fdr?\n")
            if filename:
                sys.stderr.write(
                    "Parameter filename ignored when threshold is specified.\n"
                    "Did you want to use fdr?\n")

        if genome and filename:
            raise ValueError("Need either genome or filename.")
    
        if fdr:
            fdr = float(fdr)
            if not (0.0 < fdr < 1.0):
                raise ValueError("Parameter fdr should be between 0 and 1")
        
        thresholds = {}
        motifs = read_motifs(open(self.motifs))

        if threshold:
            self.threshold = parse_threshold_values(self.motifs, threshold) 
            return
        
        if filename:
            if not os.path.exists(filename):
                raise IOError(
                        "File {} does not exist.".format(filename)
                        )
            
            bg_hash = file_checksum(filename)
            seqs = Fasta(filename).seqs
        elif genome:
            bg_hash = "{}\{}".format(genome, int(length))
        else:
            raise ValueError("Need genome or filename")

        with Cache(CACHE_DIR) as cache:
            scan_motifs = []
            for motif in motifs:
                k = "{}|{}|{:.4f}".format(motif.hash(), bg_hash, fdr)
           
                threshold = cache.get(k)
                if threshold is None:
                    scan_motifs.append(motif)
                else:
                    if np.isclose(threshold, motif.pwm_max_score()):
                        thresholds[motif.id] = None
                    else:
                        thresholds[motif.id] = threshold
                
            if len(scan_motifs) > 0:
                print scan_motifs
                if genome:
                    check_genome(genome)    
                    sys.stderr.write("Determining threshold for fdr {} and length {} based on {}\n".format(fdr, int(length), genome))
                    index = os.path.join(config.get_index_dir(), genome)
                    fa = RandomGenomicFasta(index, length, 10000)
                    seqs = fa.seqs
                else: 
                    sys.stderr.write("Determining threshold for fdr {} based on {}\n".format(fdr, filename))
                for motif, threshold in self._threshold_from_seqs(scan_motifs, seqs, fdr):
                    k = "{}|{}|{:.4f}".format(motif.hash(), bg_hash, fdr)
                    cache.set(k, threshold)
                    if np.isclose(threshold, motif.pwm_max_score()):
                        thresholds[motif.id] = None
                    else:
                        thresholds[motif.id] = threshold

        self.threshold = thresholds

    def set_genome(self, genome):
        """
        set the genome to be used for:
            - converting regions to sequences
            - background for MOODS
        """
        index_dir = os.path.join(self.config.get_index_dir(), genome)
        if not os.path.exists(index_dir) or not os.path.isdir(index_dir):
            raise ValueError("index for {} does not exist".format(genome))
        self.index_dir = index_dir
    
    def count(self, seqs, nreport=100, scan_rc=True):
        """
        count the number of matches above the cutoff
        returns an iterator of lists containing integer counts
        """
        for matches in self.scan(seqs, nreport, scan_rc):
            counts = [len(m) for m in matches]
            yield counts
     
    def total_count(self, seqs, nreport=100, scan_rc=True, cutoff=0.95):
        """
        count the number of matches above the cutoff
        returns an iterator of lists containing integer counts
        """
        
        count_table = [counts for counts in self.count(seqs, nreport, scan_rc, cutoff)]
        return np.sum(np.array(count_table), 0)

    def best_score(self, seqs, scan_rc=True):
        """
        give the score of the best match of each motif in each sequence
        returns an iterator of lists containing floats
        """
        for matches in self.scan(seqs, 1, scan_rc, cutoff=0):
            scores = [sorted(m, lambda x,y: 
                                    cmp(y[0], x[0])
                                    )[0][0] for m in matches]
            yield scores
 
    def best_match(self, seqs, scan_rc=True):
        """
        give the best match of each motif in each sequence
        returns an iterator of nested lists containing tuples:
        (score, position, strand)
        """
        for matches in self.scan(seqs, 1, scan_rc, cutoff=0):
            top = [sorted(m, lambda x,y: 
                                    cmp(y[0], x[0])
                                    )[0] for m in matches]
            yield top
    
   
    def scan(self, seqs, nreport=100, scan_rc=True):
        """
        scan a set of regions / sequences
        """

        
        if not self.threshold:
            sys.stderr.write(
                "Using default threshold of 0.95. "
                "This is likely not optimal!\n"
                )
            self.set_threshold(threshold=0.95)

        # determine input type
        seqs_type = get_seqs_type(seqs)
        
        # Fasta object
        if seqs_type.startswith("fasta"):
            if seqs_type.endswith("file"):
                seqs = Fasta(seqs)
            
            it = self._scan_sequences(seqs.seqs, 
                    nreport, scan_rc)
        # regions or BED
        else:
            if seqs_type == "regionfile":
                seqs = [l.strip() for l in open(seqs)]
            it = self._scan_regions(seqs, 
                    nreport, scan_rc)
        
        for result in it:
            yield result


    def _scan_regions(self, regions, nreport, scan_rc):
        index_dir = self.index_dir
        motif_file = self.motifs
        motif_digest = self.checksum.get(motif_file, None)

        # determine which regions are not in the cache 
        scan_regions = regions
        if self.use_cache:
            scan_regions = []
            for region in regions:
                key = str((region, index_dir, motif_digest, nreport, scan_rc))
                ret = self.cache.get(key)
                if ret == NO_VALUE:
                    scan_regions.append(region)
        
        # scan the regions that are not in the cache
        if len(scan_regions) > 0:
            n = int(MotifConfig().get_default_params()["ncpus"])
            
            genome_index = GenomeIndex(index_dir)
           
            motifs = [(m, self.threshold[m.id]) for m in read_motifs(open(self.motifs))]
            scan_func = partial(scan_region_mult,
                genome_index=genome_index,
                motifs=motifs,
                nreport=nreport,
                scan_rc=scan_rc)
    
            jobs = []
            chunksize = len(scan_regions) / n + 1
            for i in range(n):
                job = pool.apply_async(scan_func, (scan_regions[i * chunksize:( i+ 1) * chunksize],))
                jobs.append(job)
            
            # return values or store values in cache
            i = 0
            for job in jobs:
                for ret in job.get():
                    if self.use_cache:
                        # store values in cache    
                        region = scan_regions[i]
                        key = str((region, index_dir, motif_digest, nreport, scan_rc, cutoff))
                        self.cache.set(key, ret)
                    else:
                        #return values
                        yield ret
                    i += 1
    
        if self.use_cache: 
            # return results from cache
            for region in regions:
                key = str((region, index_dir, motif_digest, nreport, scan_rc, cutoff))
                ret = self.cache.get(key)
                if ret == NO_VALUE or ret is None:
                    raise Exception("cache is not big enough to hold all " 
                                    "results, try increasing the cache size "
                                    "or disable cache")
                yield ret
   

    def _scan_sequences_with_motif(self, motifs, seqs, nreport, scan_rc):
        n = int(MotifConfig().get_default_params()['ncpus'])

        scan_func = partial(scan_seq_mult,
            motifs=motifs,
            nreport=nreport,
            scan_rc=scan_rc)

        jobs = []
        
        chunksize = 500
        if len(seqs) / n + 1 < chunksize:
            chunksize = len(seqs) / n + 1
        
        for i in range((len(seqs) - 1) / chunksize + 1):
            job = pool.apply_async(scan_func, (seqs[i * chunksize:(i  + 1) * chunksize],))
            jobs.append(job)
        
        for job in jobs:
            for ret in job.get():
                yield ret 

    def _scan_sequences(self, seqs, nreport, scan_rc):
        
        motif_file = self.motifs
        motif_digest = self.checksum.get(motif_file, None)
        
        scan_seqs = seqs
        if self.use_cache:
            # determine which sequences are not in the cache 
            hashes = dict([(s.upper(), CityHash64(s.upper())) for s in seqs])
            scan_seqs = []
        
            for seq,seq_hash in hashes.items():
                key = str((seq_hash, motif_digest, nreport, scan_rc, cutoff))
                ret = self.cache.get(key)
                if ret == NO_VALUE or ret is None:
                    scan_seqs.append(seq.upper())
        
        # scan the sequences that are not in the cache
        if len(scan_seqs) > 0:
            # store values in cache
            i = 0
            motifs = [(m, self.threshold[m.id]) for m in read_motifs(open(self.motifs))]
            for ret in self._scan_sequences_with_motif(motifs, seqs, nreport, scan_rc):
               if self.use_cache:
                   h = hashes[scan_seqs[i]]
                   key = str((h, motif_digest, nreport, scan_rc, cutoff))
                   self.cache.set(key, ret)
               else: 
                    yield ret
               i += 1
        
        if self.use_cache:
            # return results from cache
            for seq in seqs:
                key = str((hashes[seq.upper()], motif_digest, nreport, scan_rc, cutoff))
                ret = self.cache.get(key)
                if ret == NO_VALUE or ret is None:
                    raise Exception("cache is not big enough to hold all " 
                                    "results, try increasing the cache size "
                                    "or disable cache")
                    
                yield ret

try: 
    from gimmemotifs.mp import pool
except ImportError:
    pass
