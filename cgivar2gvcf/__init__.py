#!/usr/bin/python
# Filename: cgivar2vcf.py
"""Conversion of Complete Genomics, Inc. (CGI) var files to VCF files."""
from __future__ import unicode_literals
import argparse
import bz2
from collections import OrderedDict
import datetime
import gzip
import os
import re
import sys
from numpy import median

import twobitreader
from twobitreader import download as twobitdownload


VCF_DATA_TEMPLATE = OrderedDict([
    ('CHROM', None),
    ('POS', None),
    ('ID', '.'),
    ('REF', None),
    ('ALT', '.'),
    ('QUAL', '.'),
    ('FILTER', '.'),
    ('INFO', '.'),
    ('FORMAT', '.'),
    ('SAMPLE', '.')
])

FILEDATE = datetime.datetime.now()


def make_header(reference):
    header = """##fileformat=VCFv4.2
##fileDate={}{}{}
##source=cgivar2gvcf-version-0.1.6.1
##description="Produced from a Complete Genomics varFile using a modified version of cgivar2gvcf" Not intended for clinical use."
##reference={}
##FILTER=<ID=PASS,Description="All filters passed">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=FT,Number=R,Type=String,Description="varFilter Tag (VQLOW, AMBIGUOUS, PASS, VQHIGH and NOCALL) from Complete Genomics varFile. VQHIGH is specific to CG version 2.2 or lower, in CG version 2.4 or higher the equivalent is PASS. AMBIGUOUS is specific of CG version 2.4 of higher">
##FORMAT=<ID=VAF,Number=R,Type=Integer,Description="Positive integer representing confidence in the call as reported in the varScoreVAF of Complete Genomics. It is derived from the probability estimates under maximum likelihood variable allele fraction. This field is empty for reference calls or no-calls">
##FORMAT=<ID=EAF,Number=R,Type=Integer,Description="Positive or negative integer representing confidence in the call as reported in the varScoreEAF of Complete Genomics. It is derived from the probability estimates under equal allele fraction model. This field is empty for reference calls or no-calls">
##INFO=<ID=END,Number=1,Type=Integer,Description="Stop position of the interval">
""".format(FILEDATE.year, FILEDATE.month, FILEDATE.day, reference)
    header = header + ("#" + '\t'.join([k for k in VCF_DATA_TEMPLATE]))
    return header


def auto_zip_open(filepath, mode):
    """Convenience function for opening potentially-compressed files."""
    if filepath.endswith('.gz'):
        outfile = gzip.open(filepath, mode)
    elif filepath.endswith('.bz2'):
        outfile = bz2.BZ2File(filepath, mode)
    else:
        outfile = open(filepath, mode)
    return outfile


def formatted_vcf_line(vcf_data):
    return '\t'.join([vcf_data[k] for k in vcf_data])


def process_full_position(data, header, var_only=False):
    """
    Return genetic data when all alleles called on same line.

    Returns an array containing one item, a tuple of five items:
        (string) chromosome
        (string) start position (1-based)
        (array of strings) matching dbSNP entries
        (string) reference allele sequence
        (array of strings) the genome's allele sequences
    """
    feature_type = data[header['varType']]
    # Skip unmatchable, uncovered, or pseudoautosomal-in-X
    # This clause will skip the N portions of the genome
    if (feature_type == 'no-ref' or feature_type.startswith('PAR-called-in-X')):
        return None
    if var_only and feature_type in ['no-call', 'ref']:
        return None

    filters = []
    if feature_type == 'no-call':
        filters.append('NOCALL')

    # This 'if' is entered if the CG format is older than the 2.4 version
    if 'varQuality' in header:
        if 'VQLOW' in data[header['varQuality']]:
            filters.append('VQLOW')

        if 'VQHIGH' in data[header['varQuality']]:
            filters.append('VQHIGH')

    # And, if the CG format is 2.4 or older version ->
    else:
        var_filter = data[header['varFilter']]
        if var_filter and not var_filter == "PASS":
            filters = filters + var_filter.split(';')

    chrom = data[header['chromosome']]
    start = data[header['begin']]
    ref_allele = data[header['reference']]
    alleles = [data[header['alleleSeq']]]
    dbsnp_data = []
    dbsnp_data = data[header['xRef']].split(';')
    var_scores = []

    if data[header['varScoreVAF']] or data[header['varScoreEAF']]:
        var_scores.append(data[header['varScoreVAF']])
        var_scores.append(data[header['varScoreEAF']])

    assert data[header['ploidy']] in ['1', '2']
    if feature_type == 'ref' or feature_type == 'no-call':
        return [{'chrom': chrom,
                 'start': start,
                 'dbsnp_data': dbsnp_data,
                 'ref_seq': ref_allele,
                 'alleles': alleles,
                 'varScores': var_scores,
                 'allele_count': data[header['ploidy']],
                 'filters': [filters],
                 'end': data[header['end']]}]
    else:

        return [{'chrom': chrom,
                 'start': start,
                 'dbsnp_data': dbsnp_data,
                 'ref_seq': ref_allele,
                 'alleles': alleles,
                 'allele_count': data[header['ploidy']],
                 'varScores': var_scores,
                 'filters': [filters]}]


def process_allele(allele_data, dbsnp_data, header, reference):
    """Combine data from multiple lines referring to a single allele.

    Returns three items in this order:
        (string) concatenated variant sequence (ie allele the genome has)
        (string) concatenated reference sequence
        (string) start position (1-based)
    """
    # One-based start to match VCF coordinates
    start = str(int(allele_data[0][header['begin']]))
    var_allele = ''
    ref_allele = ''
    filters = []
    VAF_scores = []
    EAF_scores = []
    vaf_score = ''
    eaf_score = ''

    for data in allele_data:
        # This 'if' is entered if the CG format is older than the 2.4 version
        # only two values are possible in this version: VQLOW and VQHIGH
        if 'varQuality' in header:
            if 'VQLOW' in data[header['varQuality']]:
                filters.append('VQLOW')
            if 'VQHIGH' in data[header['varQuality']]:
                filters.append('VQHIGH')
        # And, this 'else' is entered if the CG format is 2.4 or 2.5 version ->
        else:
            var_filter = data[header['varFilter']]
            if var_filter and not var_filter == "PASS":
                # filters = filters + var_filter.split(';')
                filters = filters + var_filter.split(';')
        if data[header['varType']] == 'no-call':
            filters = ['NOCALL']
            ref_allele = ref_allele + data[header['reference']]
            continue
        var_allele = var_allele + data[header['alleleSeq']]
        ref_allele = ref_allele + data[header['reference']]
        if data[header['xRef']]:
            for dbsnp_item in data[header['xRef']].split(';'):
                dbsnp_data.append(dbsnp_item.split(':')[1])
        if data[header['varScoreVAF']] and data[header['varScoreEAF']]:
            VAF_scores.append(int(data[header['varScoreVAF']]))
            EAF_scores.append(int(data[header['varScoreEAF']]))
        # gets the median value of all VAF and all EAF scores, in separate
    if data[header['varScoreVAF']] and data[header['varScoreEAF']]:
        vaf_score = str(int(median(VAF_scores)))
        eaf_score = str(int(median(EAF_scores)))
    else:
        vaf_score = ''
        eaf_score = ''
    #manage gathered filters
    # It's theoretically possible to break up a partial no-call allele into
    # separated gVCF lines, but it's hard. Treat the whole allele as no-call.
    if 'NOCALL' in filters:
        filters = ['NOCALL']
        var_allele = '?'
        vaf_score = ''
        eaf_score = ''
    return var_allele, ref_allele, start, filters, vaf_score, eaf_score


def get_split_pos_lines(data, cgi_input, header):
    """Advance across split alleles and return data from each.

    CGI var file reports alleles separately for heterozygous sites:
    all variant or reference information is called for the first allele,
    then for the second. This function moves forward in the file to
    get lines for each (and ends up with one remaineder line as well).
    """
    s1_data = [data]
    s2_data = []
    next_data = cgi_input.readline().decode('utf-8').rstrip('\n').split("\t")
    while next_data[header['allele']] == "1":
        s1_data.append(next_data)
        next_data = cgi_input.readline().decode('utf-8').rstrip('\n').split("\t")
    while next_data[header['allele']] == "2":
        s2_data.append(next_data)
        next_data = cgi_input.readline().decode('utf-8').rstrip('\n').split("\t")
    return s1_data, s2_data, next_data


def process_split_position(data, cgi_input, header, reference, var_only=False):
    """Process CGI var where alleles are reported separately.

    Split positions report each allele with one or more lines. To ensure that
    we've read through all lines, we end up reading one line beyond.

    This function returns data for this position, then handles the remainder
    line by calling itself or process_full_position (as appropriate).

    Returns an array containing tuples with five items each:
        (string) chromosome
        (string) start position (1-based)
        (array of strings) matching dbSNP entries
        (string) reference allele sequence
        (array of strings) the genome's allele sequences
    """
    assert data[2] == "1"
    chrom = data[header['chromosome']]

    # Get all lines for each allele. Note that this means we'll end up with
    # data from one line ahead stored in 'next_data'; it will be handled at
    # the end.
    s1_data, s2_data, next_data = get_split_pos_lines(
        data=data, cgi_input=cgi_input, header=header)

    # Process all the lines to get concatenated sequences and other data.
    dbsnp_data = []

    a1_seq, ref_seq, start, a1_filters, a1_vaf_score, a1_eaf_score = process_allele(
        allele_data=s1_data, dbsnp_data=dbsnp_data,
        header=header, reference=reference)
    a2_seq, r2_seq, a2_start, a2_filters, a2_vaf_score, a2_eaf_score = process_allele(
        allele_data=s2_data, dbsnp_data=dbsnp_data,
        header=header, reference=reference)

    # clean dbsnp data
    dbsnp_data = [x for x in dbsnp_data if x]

    if (a1_seq or ref_seq) and (a2_seq or r2_seq):
        # Check that reference sequence and positions match.
        assert ref_seq == r2_seq
        assert start == a2_start
        if (a1_seq != '?') or (a2_seq != '?'):
            yield {'chrom': chrom,
                   'start': start,
                   'dbsnp_data': dbsnp_data,
                   'ref_seq': ref_seq,
                   'alleles': [a1_seq, a2_seq],
                   'varScores': [a1_vaf_score, a1_eaf_score, a2_vaf_score, a2_eaf_score],
                   'allele_count': '2',
                   'filters': [a1_filters, a2_filters]} # -> 'filters': list(set(a1_filters + a2_filters))}
            # 'filters': list(set(a1_filters + a2_filters))}
        else:
            # Handle edge case: because we create full no-calls from partial
            # no-call alleles, we may end up with a full no-call region.
            end = str(int(start) + len(ref_seq))
            yield {'chrom': chrom,
                    'start': start,
                    'dbsnp_data': [],
                    'ref_seq': '=',
                    'alleles': ['?'],
                    'allele_count': '2',
                    'varScores': [a1_vaf_score, a1_eaf_score, a2_vaf_score, a2_eaf_score],
                    'filters': [a1_filters, a2_filters], # before -> ['NOCALL']
                    'end': end}

    # Handle the remaining line. Could recursively call this function if it's
    # the start of a new split position - very unlikely, though.
    if next_data[2] == "all" or next_data[1] == "1":
        out = process_full_position(
            data=next_data, header=header, var_only=var_only)
    else:
        out = process_split_position(
            data=next_data, cgi_input=cgi_input, header=header,
            reference=reference, var_only=var_only)
    if out:
        for entry in out:
            yield entry


def vcf_line(input_data, reference):
    """
    Convert the var files information into VCF format.

    This is nontrivial because the var file can contain zero-length variants,
    which is not allowed by VCF. To handle these cases, we "move backwards"
    by one position, look up the reference sequence, and add that.

    The returned line is a very simple, VCF-valid row containing the
    genome's data for this position.
    """
    vcf_data = VCF_DATA_TEMPLATE.copy()
    start = int(input_data['start'])
    dbsnp_data = input_data['dbsnp_data']
    ref_allele = input_data['ref_seq']
    genome_alleles = input_data['alleles']

    # Get dbSNP IDs.
    dbsnp_cleaned = []
    for dbsnp in dbsnp_data:
        if dbsnp not in dbsnp_cleaned:
            dbsnp_cleaned.append(dbsnp)
    if dbsnp_cleaned:
        id_field = ';'.join(dbsnp_cleaned)
        if id_field == '':
            id_field = '.'
    else:
        id_field = '.'

    format = 'GT:FT'
    sample = []

    # Is this a matching reference line? Handle per gVCF spec.
    if input_data['ref_seq'] == '=':
        ref_allele = reference[input_data['chrom']][start].upper()
        vcf_data['CHROM'] = input_data['chrom']

        # Position notes: Complete Genomics uses 0-based start and 1-based end.
        # Reference seq retrieval is 0-based start, but VCF is 1-based start.
        vcf_data['POS'] = str(start + 1)
        vcf_data['ID'] = id_field
        vcf_data['REF'] = ref_allele
        vcf_data['ALT'] = '.'

        assert input_data['allele_count'] in ['1', '2']
        if '?' in input_data['alleles']:

            if ['NOCALL'] not in input_data['filters']:
                input_data['filters'].append('NOCALL')

            if input_data['allele_count'] == '2':
                sample.append('./.')

            else:
                sample.append('.')

        elif input_data['allele_count'] == '2':
            sample.append('0/0')

        else:
            sample.append('0')

        if input_data['filters'] and input_data['allele_count'] == '2':

            if input_data['alleles'] in [['?'],['=']] and input_data['filters'] != [[]]:
                a1_filter = input_data['filters'][0][0]
                a2_filter = input_data['filters'][0][0]
                sample.append(a1_filter + ',' + a2_filter)

            elif input_data['filters'] == [[]]:
                a1_filter = 'PASS'
                a2_filter = 'PASS'
                sample.append(a1_filter + ',' + a2_filter)

            else:

                if 'NOCALL' in input_data['filters'][0]:
                    a1_filter = 'NOCALL'
                elif 'VQLOW' in input_data['filters'][0]:
                    a1_filter = 'VQLOW'
                elif 'AMBIGUOUS' in input_data['filters'][0]:
                    a1_filter = 'AMBIGUOUS'
                elif 'VQHIGH' in input_data['filters'][0]:
                    a1_filter = 'VQHIGH'
                else:
                    a1_filter = 'PASS'

                # print(input_data)
                if 'NOCALL' in input_data['filters'][1]:
                    a2_filter = 'NOCALL'
                elif 'VQLOW' in input_data['filters'][1]:
                    a2_filter = 'VQLOW'
                elif 'AMBIGUOUS' in input_data['filters'][1]:
                    a2_filter = 'AMBIGUOUS'
                elif 'VQHIGH' in input_data['filters'][1]:
                    a2_filter = 'VQHIGH'
                else:
                    a2_filter = 'PASS'

                sample.append(a1_filter + ',' + a2_filter)

            # sample.append(';'.join(input_data['filters']))
            vcf_data['FILTER'] = '.'

        elif input_data['filters'] and input_data['allele_count'] == '1':

            if 'NOCALL' in input_data['filters'][0]:
                a1_filter = 'NOCALL'
            elif 'VQLOW' in input_data['filters'][0]:
                a1_filter = 'VQLOW'
            elif 'AMBIGUOUS' in input_data['filters'][0]:
                a1_filter = 'AMBIGUOUS'
            elif 'VQHIGH' in input_data['filters'][0]:
                a1_filter = 'VQHIGH'
            else:
                a1_filter = 'PASS'

            vcf_data['FILTER'] = '.'
            sample.append(a1_filter)

        elif not input_data['filters'] and input_data['allele_count'] == '2':
            sample.append('PASS,PASS')
            vcf_data['FILTER'] = '.'

        else: # equivalent to -> elif not input_data['filters'] and input_data['allele_count'] == '1':
            sample.append('PASS')
            vcf_data['FILTER'] = '.'

        if input_data['varScores'] and input_data['varScores'] not in [['','','','']]:
            format = format + ':VAF:EAF'
            sample.append(':'.join(input_data['varScores']))

        vcf_data['SAMPLE'] = ':'.join(sample)
        vcf_data['INFO'] = 'END={}'.format(input_data['end'])
        vcf_data['FORMAT'] = format

        return formatted_vcf_line(vcf_data)

    # VCF doesn't allow zero-length sequences. If we have this situation,
    # move the start backwards by one position, get that reference base,
    # and prepend this base to all sequences.
    if len(ref_allele) == 0 or 0 in [len(v) for v in genome_alleles]:
        start = start - 1
        prepend = reference[input_data['chrom']][start].upper()
        ref_allele = prepend + ref_allele
        genome_alleles = [prepend + v if v != '?' else v for v in
                          genome_alleles]

    # Figure out what our alternate alleles are.
    alt_alleles = []
    for allele in genome_alleles:
        if allele not in [ref_allele] + alt_alleles and allele != '?':
            alt_alleles.append(allele)

    # Combine ref and alt for the full set of alleles, used for indexing.
    alleles = [ref_allele] + alt_alleles

    # Get the indexed genotype.
    allele_indexes = [str(alleles.index(x)) for x in genome_alleles if
                      x != '?']
    [allele_indexes.append('.') for x in genome_alleles if x == '?']
    genotype = '/'.join(allele_indexes)

    vcf_data['CHROM'] = input_data['chrom']
    vcf_data['POS'] = str(start + 1)
    vcf_data['ID'] = id_field
    vcf_data['REF'] = ref_allele
    vcf_data['ALT'] = ','.join(alt_alleles)

    sample.append(genotype)

    if input_data['filters'] and input_data['allele_count'] == '2':
        # vcf_data['FILTER'] = ';'.join(sorted(input_data['filters']))
        # assert 'NOCALL' not in input_data['filters'][0] and 'NOCALL' not in input_data['filters'][1]
        if 'NOCALL' in input_data['filters'][0]:
            a1_filter = 'NOCALL'
        elif 'VQLOW' in input_data['filters'][0]:
            a1_filter = 'VQLOW'
        elif 'AMBIGUOUS' in input_data['filters'][0]:
            a1_filter = 'AMBIGUOUS'
        elif 'VQHIGH' in input_data['filters'][0]:
            a1_filter = 'VQHIGH'
        else:
            a1_filter = 'PASS'

        if 'NOCALL' in input_data['filters'][1]:
            a2_filter = 'NOCALL'
        elif 'VQLOW' in input_data['filters'][1]:
            a2_filter = 'VQLOW'
        elif 'AMBIGUOUS' in input_data['filters'][1]:
            a2_filter = 'AMBIGUOUS'
        elif 'VQHIGH' in input_data['filters'][1]:
            a2_filter = 'VQHIGH'
        else:
            a2_filter = 'PASS'

        sample.append(a1_filter + ',' + a2_filter)

        vcf_data['FILTER'] = '.'

    elif input_data['filters'] and input_data['allele_count'] == '1':

        if 'VQLOW' in input_data['filters'][0]:
            a1_filter = 'VQLOW'
        elif 'AMBIGUOUS' in input_data['filters'][0]:
            a1_filter = 'AMBIGUOUS'
        elif 'VQHIGH' in input_data['filters'][0]:
            a1_filter = 'VQHIGH'
        else:
            a1_filter = 'PASS'

        vcf_data['FILTER'] = '.'
        sample.append(a1_filter)

    elif not input_data['filters'] and input_data['allele_count'] == '2':
        sample.append('PASS,PASS')
        vcf_data['FILTER'] = '.'

    else:   # equivalent to -> elif not input_data['filters'] and input_data['allele_count'] == '1':
        sample.append('PASS')
        vcf_data['FILTER'] = '.'

    if input_data['varScores'] and input_data['varScores'] not in [['','','','']]:

        assert len(input_data['varScores']) == 2 or len(input_data['varScores']) == 4
        format = format + ':VAF:EAF'

        if len(input_data['varScores']) == 2:
            sample.append(':'.join(input_data['varScores']))

        if len(input_data['varScores']) == 4:

            #if a1_vaf_score and a1_eaf_score are missing -> VAF:EAF -> .,X:X,.
            if input_data['varScores'][0] == '' and input_data['varScores'][1] == '':
                sample.append('.,' + input_data['varScores'][2])
                sample.append('.,' + input_data['varScores'][3])

            elif input_data['varScores'][2] == '' and input_data['varScores'][3] == '':
                sample.append(input_data['varScores'][0] + ',.')
                sample.append(input_data['varScores'][1] + ',.')

            else:
                sample.append(','.join(input_data['varScores'][:2]))
                sample.append(','.join(input_data['varScores'][2:4]))

    vcf_data['SAMPLE'] = ':'.join(sample)
    vcf_data['FORMAT'] = format

    return formatted_vcf_line(vcf_data)


def process_next_position(data, cgi_input, header, reference, var_only):
    """
    Determine appropriate processing to get data, then convert it to VCF

    There are two types of lines in the var file:
    - "full position": single allele (hemizygous) or all-allele line
        All alleles at this position are represented in this line.
        This is handled with "process_full_position".
    - "split position": each of two alleles is reported separately. There will
        be at least two lines, one for each allele (but potentially more).
        This is handled with "process_split_position".

    Because the number of lines used for separately reported alleles is
    unknown, process_split_position will always read ahead to the next
    "full position" and return that as well.

    So the returned line formats are consistent, process_next_position
    returns an array, even if there's only one line.
    """
    if data[2] == "all" or data[1] == "1":
        # The output from process_full_position is an array, so it can be
        # treated in the same manner as process_split_position output.
        out = process_full_position(data=data, header=header, var_only=var_only)
    else:
        assert data[2] == "1"
        # The output from process_split_position is a generator, and may end
        # up calling itself recursively.
        out = process_split_position(
            data=data, cgi_input=cgi_input, header=header, reference=reference, var_only=var_only)
    if out:

        # ChrM is skipped because Complete Genomics is using a different
        # reference than UCSC's reference. Their documentation states:
        #   The version we use, "build 37," consists of the assembled nuclear
        #   chromosomes from GRCh37 (not unplaced or alternate loci), plus the
        #   Cambridge Reference Sequence for the mitochondrion (NC_012920.1).
        #   This assembly (though with an alternate mitochondrial sequence) is
        #   also known as UCSC hg19.
        return [vcf_line(input_data=l, reference=reference) for l in out if
                l['chrom'] != 'chrM']


def convert(cgi_input, twobit_ref, twobit_name, var_only=False):
    """Generator that converts CGI var data to VCF-formated strings"""

    # Set up CGI input. Default is to assume a str generator.
    if isinstance(cgi_input, str) or isinstance(cgi_input, unicode):
        cgi_input = auto_zip_open(cgi_input, 'rb')

    # Set up TwoBitFile for retrieving reference sequences.
    reference = twobitreader.TwoBitFile(twobit_ref)

    # Output header.
    header = make_header(twobit_name).split('\n')
    for line in header:
        yield line

    while True:
        line = cgi_input.readline()
        if not line:
            break
        line = line.decode('utf-8')

        # Skip header lines.
        if re.search(r'^\W*$', line) or line.startswith('#'):
            continue

        # Store header row labels.
        if line.startswith('>'):
            header_data = line.lstrip('>').rstrip('\n').split('\t')
            header = {header_data[i]: i for i in range(len(header_data))}
            continue

        # If we reach this point, this is a line that contains data.
        data = line.rstrip('\n').split("\t")

        out = process_next_position(
            data=data, cgi_input=cgi_input, header=header, reference=reference,
            var_only=var_only)

        # process_next_position returns an array of one or more lines
        if out:
            for line in out:
                yield line


def convert_to_file(cgi_input, output_file, twobit_ref, twobit_name, var_only=False):
    """Convert a CGI var file and output VCF-formatted data to file"""

    if isinstance(output_file, str):
        output_file = auto_zip_open(output_file, 'w')

    conversion = convert(cgi_input=cgi_input, twobit_ref=twobit_ref, twobit_name=twobit_name, var_only=var_only)
    for line in conversion:
        output_file.write(line + "\n")
    output_file.close()


def get_reference_genome_file(refseqdir, build):
    """
    Convenience fxn to get reference genome from target dir, download if needed
    """
    if not os.path.exists(refseqdir) or not os.path.isdir(refseqdir):
        raise ValueError("No directory at {}".format(refseqdir))
    twobit_name = ''
    if build in ['b37', 'build 37', 'build37', '37', 'hg19']:
        twobit_name = 'hg19.2bit'
        build = 'build37'
    if not twobit_name:
        raise ValueError('Genome bulid "{}" not supported.'.format(build))
    twobit_path = os.path.join(refseqdir, twobit_name)
    if not os.path.exists(twobit_path):
        twobitdownload.save_genome('hg19', destdir=refseqdir)
    return twobit_path, twobit_name


def from_command_line():
    """
    Run CGI var to gVCF conversion from the command line.
    """
    # Parse options
    parser = argparse.ArgumentParser(
        description='Convert Complete Genomics var files to gVCF format.')
    parser.add_argument(
        '-d', '--refseqdir', metavar='REFSEQDIR', required=True,
        dest='refseqdir',
        help='Directory twobit reference genomes files are stored.')
    parser.add_argument(
        '-i', '--input', metavar='INPUTVARFILE',
        dest='cgivarfile',
        help='Path to Complete Genomics var file to convert. If omitted, data '
        ' also be piped in as standard input.')
    parser.add_argument(
        '-o', '--output', metavar='OUTPUTVCFFILE',
        dest='vcfoutfile',
        help='Path to where to save output VCF file.')
    parser.add_argument(
        '-D', '--download', action='store_true', dest='downloadrefseq',
        help='Download the 2bit file from UCSC to REFSEQDIR, if needed.')
    parser.add_argument(
        '-v', '--var-only', action='store_true', dest='varonly',
        help='Only report variant lines (i.e. VCF, but not gVCF)')
    args = parser.parse_args()

    # Get local twobit file from its directory. Download and store if needed.
    twobit_path, twobit_name = get_reference_genome_file(
        args.refseqdir, build='b37')
    # Handle input
    if sys.stdin.isatty():  # false if data is piped in
        var_input = args.cgivarfile
    else:
        var_input = sys.stdin
    # Handle output
    if args.vcfoutfile:
        convert_to_file(var_input,
                        args.vcfoutfile,
                        twobit_path,
                        twobit_name,
                        args.varonly)
    else:
        for line in convert(
                cgi_input=var_input,
                twobit_ref=twobit_path,
                twobit_name=twobit_name,
                var_only=args.varonly):
            print(line)


if __name__ == '__main__':
    from_command_line()
