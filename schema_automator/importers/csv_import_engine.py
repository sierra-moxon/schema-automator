import click
import logging
import yaml
from typing import Dict, List, Optional
from collections import defaultdict
import os
import re
import csv
import requests
import pandas as pd
import time

from dateutil.parser import parse
from quantulum3 import parser as q_parser
from dataclasses import dataclass, field
from schema_automator.importers.import_engine import ImportEngine
from schema_automator.utils.schemautils import merge_schemas

ID_SUFFIX = '_id'

ROBOT_NAME_MAP = {
    'alternative term': "IAO:0000118",
    'definition': "IAO:0000115",
    'definition_source': "IAO:0000119",
}


@dataclass
class ForeignKey:
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    num_distinct_values: int
    range: str = None

    def score(self):
        s = 0
        if self.source_table == self.target_table:
            s -= 1
        if self.source_column.lower().endswith(ID_SUFFIX):
            s += 1
        if self.target_column.lower().endswith(ID_SUFFIX) or self.target_column.lower() == 'id':
            s += 1
        if self.source_column.lower().startswith(self.target_table.lower()):
            s += 2
        if self.range != 'string' and self.range != 'integer':
            s -= 1
        return s


@dataclass
class CsvDataImportEngine(ImportEngine):

    file_separator: str = "\t"
    schema_name: str = 'example'
    robot: bool = False
    enum_columns: List[str] = field(default_factory=lambda: [])
    enum_mask_columns: List[str] = field(default_factory=lambda: [])
    enum_threshold: float = 0.1
    enum_strlen_threshold: int = 30
    max_enum_size: int = 50
    downcase_header: bool = False
    infer_foreign_keys: bool = False
    max_pk_len: int = 60   # URIs can be long..
    min_distinct_fk_val: int = 8

    def infer_linkages(self, files: List[str], **kwargs) -> List[ForeignKey]:
        """
        Heuristic procedure for determining which tables are linked to others via implicit foreign keys

        If all values of one column FT.FC are present in column PT.PC, then FT.FC is a potential foreign key
        and PC is a potential primary key of PT.

        This procedure can generate false positives, so additional heuristics are applied. Each potential
        foreign key relationship gets an ad-hoc score:
         - links across tables score more highly than within
         - suffixes such as _id are more likely on PK and FK tables
         - the foreign key column table is likely to start with the base column name
        In addition, if there are competing primary keys for a table, the top scoring one is selected
        """
        fks: List[ForeignKey] = []
        MAX_PK_LEN = self.max_pk_len
        dfs: Dict[str, pd.DataFrame] = {}

        for file in files:
            c = os.path.splitext(os.path.basename(file))[0]
            if self.downcase_header:
                c = c.lower()
            print(f'READING {file} ')
            df = pd.read_csv(file, sep=self.file_separator, skipinitialspace=True).fillna("")
            if self.downcase_header:
                df = df.rename(columns=str.lower)
            exclude = []
            for col in df.columns:
                vals = set(df[col].tolist())
                if len(vals) < self.min_distinct_fk_val:
                    logging.info(f'EXCLUDING {col} (too few, len = {len(vals)})')
                    exclude.append(col)
                    continue
                max_str_len = max([len(str(x)) for x in vals])
                if max_str_len > MAX_PK_LEN:
                    logging.info(f'EXCLUDING {col} (len {max_str_len} > {MAX_PK_LEN}) sample: {list(vals)[0:5]}')
                    #for v in vals:
                    #    if len(str(v)) == max_str_len:
                    #        print(f'  WITNESS: {v}')
                    exclude.append(col)
                    continue
                if any(' ' in str(x) for x in vals ):
                    logging.info(f'EXCLUDING {col} (has spaces)')
                    exclude.append(col)
                    continue
            for col in exclude:
                del df[col]
                logging.info(f'Excluding: {col}')
            dfs[c] = df
        for t_primary, df_primary in dfs.items():
            for candidate_pk in df_primary.columns:
                candidate_pk_vals = set(df_primary[candidate_pk].tolist())
                candidate_pk_range = infer_range({}, candidate_pk_vals, {})
                logging.info(f'Candidate PK {t_primary}.{candidate_pk} ')
                for t_foreign, df_foreign in dfs.items():
                    logging.info(f' Candidate FK table {t_foreign} ')
                    for candidate_fk in df_foreign.columns:
                        logging.info(f'  Candidate FK col {candidate_fk} ')
                        if t_primary == t_foreign and candidate_pk == candidate_fk:
                            logging.info(f'   SKIP (identical) {candidate_fk} ')
                            continue
                        candidate_fk_vals = set(df_foreign[candidate_fk].tolist())
                        logging.info(f'    Candidate FK {t_foreign}.{candidate_fk}')
                        is_fk = True
                        for v in candidate_fk_vals:
                            if v is None or v == '':
                                continue
                            if v not in candidate_pk_vals:
                                logging.info(f'    {v} not in candidates')
                                is_fk = False
                            if not is_fk:
                                break
                        if is_fk:
                            logging.info(f'    all {len(candidate_fk_vals)} fk vals in {len(candidate_pk_vals)} pks')
                            fks.append(ForeignKey(source_table=t_foreign,
                                                  source_column=candidate_fk,
                                                  target_table=t_primary,
                                                  target_column=candidate_pk,
                                                  num_distinct_values=len(candidate_fk_vals),
                                                  range=candidate_pk_range))

        pk_tables = set([fk.target_table for fk in fks])
        for pk_table in pk_tables:
            s = defaultdict(float)
            max_s = -1000
            for fk in fks:
                if fk.target_table == pk_table:
                    s[fk.target_column] += fk.score()
                    if s[fk.target_column] > max_s:
                        max_s = s[fk.target_column]
            pk_col, _ = sorted(s.items(), key=lambda item: -item[1])[0]
            logging.info(f'SELECTED pk col {pk_col} for {pk_table}; scores = {s}')
            fks = [fk for fk in fks if not (fk.target_table == pk_table and fk.target_column != pk_col)]
        fks = [fk for fk in fks if fk.score() > 0]
        logging.info(f'FILTERED: {fks}')
        return fks

    def inject_foreign_keys(self, schema_dict: Dict, fks: List[ForeignKey]) -> None:
        for fk in fks:
            # TODO: deal with cases where the same slot is used in different classes
            src_cls = schema_dict['classes'][fk.source_table]
            src_slot = schema_dict['slots'][fk.source_column]
            if 'slot_usage' not in src_cls:
                src_cls['slot_usage'] = {}
            src_cls['slot_usage'][fk.source_column] = {'range': fk.target_table}
            #src_slot['range'] = fk.target_table
            tgt_cls = schema_dict['classes'][fk.target_table]
            tgt_slot = schema_dict['slots'][fk.target_column]
            if 'slot_usage' not in tgt_cls:
                tgt_cls['slot_usage'] = {}
            tgt_cls['slot_usage'][fk.target_column] = {'identifier': True}
            #tgt_slot['identifier'] = True

    def convert_multiple(self, files: List[str], **kwargs) -> Dict:
        if self.infer_foreign_keys:
            fks = self.infer_linkages(files)
        else:
            fks = ()
        yamlobjs = []
        for file in files:
            c = os.path.splitext(os.path.basename(file))[0]
            if self.downcase_header:
                c = c.lower()
            s = self.convert(file, class_name=c, **kwargs)
            if s is not None:
                yamlobjs.append(s)
        s = merge_schemas(yamlobjs)
        self.inject_foreign_keys(s, fks)
        return s

    def convert(self, file: str, **kwargs) -> Dict:
        with open(file, newline='') as tsv_file:
            header = [h.strip() for h in tsv_file.readline().split('\t')]
            rr = csv.DictReader(tsv_file, fieldnames=header, delimiter=self.file_separator, skipinitialspace=False)
            return self.convert_dicts([r for r in rr], **kwargs)

    def read_slot_tsv(self, file: str, **kwargs) -> Dict:
        with open(file, newline='') as tsv_file:
            rows_list = csv.reader(tsv_file, delimiter=self.file_separator)
            return self.convert_to_edge_slots([r for r in rows_list], **kwargs)

    def convert_to_edge_slots(self,
                         all_tsv_rows: List,
                         name: str = 'example',
                         **kwargs) -> Optional[Dict]:

        """
        assume that TSV has 3 relevant columns:
            1. slot name to add
            2. slot definition to add
            3. examples of values for the slot

        also assume that these are all edge_properties at the moment.
        TODO: add parameter to allow edge or node property disambiguation.
        """

        slots = {}
        slot_values = {}
        types = {}

        for item in all_tsv_rows:
            slot_name = item[0]
            slot_definition = item[1]
            slot_example_type = item[2]
            if isinstance(slot_example_type, list):
                vs = slot_example_type
            elif isinstance(slot_example_type, str):
                vs = slot_example_type.split('|')
            else:
                vs = [slot_example_type]
            if slot_name not in slots:
                slots[slot_name] = {'is_a': 'association slot', 'description': slot_definition, 'range': None}
                slot_values[slot_name] = set()
            if slot_example_type is not None and slot_example_type != "" and not str(slot_example_type).startswith('$ref:'):
                slots[slot_name]['examples'] = [{'value': slot_example_type}]
                slot_values[slot_name].update(vs)
            if len(vs) > 1:
                slots[slot_name]['multivalued'] = True

        new_slots = {}
        # slots is a dict{dict}
        for sn, s in slots.items():
            vals = slot_values[sn]
            s['range'] = infer_range(s, vals, types)
        for sn, s in new_slots.items():
            if sn not in slots:
                slots[sn] = s
        schema = {
            'slots': slots
        }
        return schema

    def convert_dicts(self,
                      rr: List[Dict],
                      name: str = 'example',
                      class_name: str = 'example',
                      **kwargs) -> Optional[Dict]:
        slots = {}
        slot_values = {}
        n = 0
        enums = {}
        robot_defs = {}
        slot_usage = {}
        types = {}
        enum_columns = self.enum_columns
        enum_mask_columns = self.enum_mask_columns
        if len(rr) == 0:
            return None
        for row in rr:
            if self.downcase_header:
                row = {k.lower(): v for k, v in row.items()}
            n += 1
            if n == 1 and self.robot:
                for k, v in row.items():
                    robot_defs[k] = v
                continue
            for k, v in row.items():
                if k is None or k == '':
                    continue
                if v is None:
                    v = ""
                if isinstance(v, str):
                    v = v.strip()
                if isinstance(v, list):
                    vs = v
                elif isinstance(v, str):
                    vs = v.split('|')
                else:
                    vs = [v]
                if k not in slots:
                    slots[k] = {'range': None}
                    slot_values[k] = set()
                if v is not None and v != "" and not str(v).startswith('$ref:'):
                    slots[k]['examples'] = [{'value': v}]
                    slot_values[k].update(vs)
                if len(vs) > 1:
                    slots[k]['multivalued'] = True
        types = {}
        new_slots = {}
        for sn, s in slots.items():
            vals = slot_values[sn]
            s['range'] = infer_range(s, vals, types)
            if (s['range'] == 'string' or sn in enum_columns) and sn not in enum_mask_columns:
                n_distinct = len(vals)
                longest = max([len(str(v)) for v in vals]) if n_distinct > 0 else 0
                if sn in enum_columns or \
                        ((n_distinct / n) < self.enum_threshold and 0 < n_distinct <= self.max_enum_size
                         and longest < self.enum_strlen_threshold):
                    enum_name = sn.replace(' ', '_').replace('(s)', '')
                    enum_name = f'{enum_name}_enum'
                    s['range'] = enum_name
                    enums[enum_name] = {
                        'permissible_values': {v:{'description': v} for v in vals}
                    }
            # ROBOT template hints. See http://robot.obolibrary.org/template
            if sn in robot_defs:
                rd = robot_defs[sn]
                if 'SPLIT' in rd:
                    rd = re.sub(' SPLIT.*', '', rd)
                if rd.startswith("EC"):
                    rd = rd.replace('EC ', '')
                    rel = capture_robot_some(rd)
                    ss = rd.replace('%', '{' + sn + '}')
                    slot_usage['equivalence axiom'] = {'string_serialization': ss}
                    if rel is not None:
                        s['is_a'] = rel
                        new_slots[rel] = {}
                elif rd.startswith("SC"):
                    rd = rd.replace('SC ', '')
                    rel = capture_robot_some(rd)
                    ss = rd.replace('%', '{' + sn + '}')
                    slot_usage['subclass axiom'] = {'string_serialization': ss}
                    if rel is not None:
                        s['is_a'] = rel
                        new_slots[rel] = {}
                        s['comments'] = ['OWL>> SomeValuesFrom']
                    else:
                        s['comments'] = ['OWL>> SubClassOf']
                elif rd.startswith("C"):
                    # TODO: semantics are dependent on CLASS_TYPE column
                    # https://robot.obolibrary.org/template
                    rd = rd.replace('C ', '')
                    if rd == '%':
                        s['broad_mappings'] = ['rdfs:subClassOf']
                    rel = capture_robot_some(rd)
                    if rel is not None:
                        s['is_a'] = rel
                        new_slots[rel] = {}
                elif rd == 'ID':
                    s['identifier'] = True
                elif rd.startswith("I"):
                    rd = rd.replace('I ', '')
                    # TODO
                elif rd == 'TYPE':
                    s['slot_uri'] = 'rdf:type'
                elif rd == 'LABEL':
                    s['slot_uri'] = 'rdfs:label'
                elif rd.startswith("A "):
                    s['slot_uri'] = rd.replace('A ', '')
                elif rd.startswith("AT "):
                    s['slot_uri'] = re.sub('^^.*', '', rd.replace('AT ', ''))
                elif rd.startswith(">A "):
                    logging.warning('Axiom annotations not supported')
                slot_uri = s.get('slot_uri', None)
                if slot_uri is not None:
                    if ' ' in slot_uri or ':' not in slot_uri:
                        if slot_uri in ROBOT_NAME_MAP:
                            s['slot_uri'] = ROBOT_NAME_MAP[slot_uri]
                        else:
                            del s['slot_uri']
                            logging.warning(f'ROBOT "A" annotations not supported yet')
        class_slots = list(slots.keys())
        for sn, s in new_slots.items():
            if sn not in slots:
                slots[sn] = s
        schema = {
            'id': f'https://w3id.org/{name}',
            'name': name,
            'description': name,
            'imports': ['linkml:types'],
            'prefixes': {
                'linkml': 'https://w3id.org/linkml/',
                name: f'https://w3id.org/{name}'
            },
            'default_prefix': name,
            'types': types,
            'classes': {
                class_name: {
                    'slots': class_slots,
                    'slot_usage': slot_usage
                }
            },
            'slots': slots,
            'enums': enums
        }
        if robot_defs:
            schema['prefixes']['IAO'] = 'http://purl.obolibrary.org/obo/IAO_'
        add_missing_to_schema(schema)
        return schema


def capture_robot_some(s: str) -> str:
    """
    parses an OWL some values from from a robot template

    :param s:
    :return:
    """
    results = re.findall('(\\S+) some %',s)
    if len(results) == 0:
        return None
    else:
        r = results[0]
        if ':' in r:
            # only use named properties
            return None
        else:
            return r


def isfloat(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def is_measurement(value):
    ms = q_parser.parse(value)
    for m in ms:
        if m.unit.name != 'dimensionless':
            return True


def is_all_measurement(values):
    """
    heuristic to guess if all values are measurements

    uses quantulum to parse

    A significant proportion must be dimensional, to avoid
    accidentally classifying a list of floats as measurements
    """
    n_dimensional = 0
    n = 0
    for value in values:
        ms = q_parser.parse(value)
        if len(ms) == 0:
            return False
        n += 1
        if all(m.unit.name != 'dimensionless' for m in ms):
            n_dimensional += 1
    # TODO: make this configurable
    if n_dimensional > n/2:
        return True
    else:
        return False


def infer_range(slot: dict, vals: set, types: dict) -> str:
    nn_vals = [v for v in vals if v is not None and v != ""]
    if len(nn_vals) == 0:
        return 'string'
    if all(str(v).startswith('$ref:') for v in nn_vals):
        return nn_vals[0].replace('$ref:', '')
    if all(isinstance(v, int) for v in nn_vals):
        return 'integer'
    if all(isinstance(v, float) for v in nn_vals):
        return 'float'
    if all(str(v).isdigit() for v in nn_vals):
        return 'integer'
    if all(is_date(v) for v in nn_vals):
        return 'datetime'
    if all(isfloat(v) for v in nn_vals):
        return 'float'
    if is_all_measurement(nn_vals):
        return 'measurement'
    v0 = nn_vals[0]
    db = get_db(v0)
    if db is not None:
        if all(get_db(v) == db for v in nn_vals):
            t = f'{db} identifier'
            types[t] = {'typeof': 'string'}
            return t
        if all(get_db(v) is not None for v in nn_vals):
            t = 'identifier'
            types[t] = {'typeof': 'string'}
            return t
    return 'string'


def get_db(db_id: str) -> str:
    parts = db_id.split(':')
    if len(parts) > 1:
        return parts[0]
    else:
        return None


def is_date(string, fuzzy=False):
    """
    Return whether the string can be interpreted as a date.

    :param string: str, string to check for date
    :param fuzzy: bool, ignore unknown tokens in string if True
    """
    try:
        parse(string, fuzzy=fuzzy)
        return True
    except Exception:
        # https://stackoverflow.com/questions/4990718/how-can-i-write-a-try-except-block-that-catches-all-exceptions
        # we don't know all the different parse exceptions, we assume any error means this is a date
        return False


@dataclass
class Hit:
    term_id: str
    name: str
    score: float


def get_pv_element(v: str, zooma_confidence: str, cache: dict = {}) -> Hit:
    """
    uses ZOOMA to guess a meaning of an enum permissible value

    :param v:
    :param zooma_confidence:
    :param cache:
    :return:
    """
    if v in cache:
        return cache[v][0]
    if zooma_confidence is None:
        return None

    def confidence_to_int(c: str) -> int:
        if c == 'HIGH':
            return 5
        elif c == 'GOOD':
            return 4
        elif c == 'MEDIUM':
            return 2
        elif c == 'LOW':
            return 1
        else:
            raise Exception(f'Unknown: {c}')
    confidence_threshold = confidence_to_int(zooma_confidence)

    ontscores = {
        'NCBITaxon': 1.0,
        'OMIT': -1.0,

    }

    # zooma doesn't seem to do much pre-processing, so we convert
    label = v
    if 'SARS-CoV' not in label:
        label = re.sub("([a-z])([A-Z])", "<1> <2>", label)  # expand CamelCase
    label = label.replace('.', ' ').replace('_', ' ')
    params = {'propertyValue': label}
    time.sleep(1)  # don't overload service
    logging.info(f'Q: {params}')
    r = requests.get('http://www.ebi.ac.uk/spot/zooma/v2/api/services/annotate',params=params)
    hits = []  # List[hit]
    for hit in r.json():
        confidence = float(confidence_to_int(hit['confidence']))
        id = hit['semanticTags'][0]
        if confidence >= confidence_threshold:
            hit = Hit(term_id=id,
                      name=hit['annotatedProperty']['propertyValue'],
                      score=confidence)
            hits.append(hit)
        else:
            logging.warning(f'Skipping {id} {confidence}')
    hits = sorted(hits, key=lambda h: h.score, reverse=True)
    logging.info(f'Hits for {label} = {hits}')
    if len(hits) > 0:
        cache[label] = hits
        return hits[0]
    else:
        return None


def convert_range(k: str, dt: str) -> str:
    t = 'string'
    if dt == 'float64':
        t = 'float'
    return t


def infer_enum_meanings(schema: dict,
                        zooma_confidence: str = 'MEDIUM',
                        cache={}) -> None:
    for _,e in schema['enums'].items():
        pvs = e['permissible_values']
        for k, pv in pvs.items():
            if pv is None:
                pv = {}
                pvs[k] = pv
            if 'meaning' not in pv or pv['meaning'] is not None:
                hit = get_pv_element(k, zooma_confidence=zooma_confidence, cache=cache)
                if hit is not None:
                    pv['meaning'] = hit.term_id
                    if 'description' not in pv:
                        pv['description'] = hit.name


def add_missing_to_schema(schema: dict):
    for slot in schema['slots'].values():
        if slot.get('range', None) == 'measurement':
            types = schema['types']
            if 'measurement' not in types:
                types['measurement'] = \
                    {'typeof': 'string',
                     'description': 'Holds a measurement serialized as a string'}


@click.group()
def main():
    pass


@main.command()
@click.argument('tsvfile')  # input TSV (must have column headers
@click.option('--output', '-o', help='Output file')
@click.option('--class_name', '-c', default='example', help='Core class name in schema')
@click.option('--schema_name', '-n', default='example', help='Schema name')
@click.option('--sep', '-s', default='\t', help='separator')
@click.option('--downcase-header/--no-downcase-header', default=False, help='if true make headers lowercase')
@click.option('--enum-columns', '-E', multiple=True, help='column that is forced to be an enum')
@click.option('--robot/--no-robot', default=False, help='set if the TSV is a ROBOT template')
def tsv2model(tsvfile, output, class_name, schema_name, **kwargs):
    """ Infer a model from a TSV """
    ie = CsvDataImportEngine(**kwargs)
    schema_dict = ie.convert(tsvfile, class_name=class_name, schema_name=schema_name)
    ys = yaml.dump(schema_dict, default_flow_style=False, sort_keys=False)
    if output:
        with open(output, 'w') as stream:
            stream.write(ys)
    else:
        print(ys)

@main.command()
@click.argument('tsvfiles', nargs=-1)  # input TSV (must have column headers
@click.option('--output', '-o', help='Output file')
@click.option('--schema_name', '-n', default='example', help='Schema name')
@click.option('--sep', '-s', default='\t', help='separator')
@click.option('--downcase-header/--no-downcase-header', default=False, help='if true make headers lowercase')
@click.option('--infer-foreign-keys/--no-infer-foreign-keys', default=False, help='infer ranges/foreign keys')
@click.option('--enum-columns', '-E', multiple=True, help='column(s) that is forced to be an enum')
@click.option('--enum-mask-columns', multiple=True, help='column(s) that are excluded from being enums')
@click.option('--max-enum-size', default=50, help='do not create an enum if more than max distinct members')
@click.option('--enum-threshold', default=0.1, help='if the number of distinct values / rows is less than this, do not make an enum')
@click.option('--robot/--no-robot', default=False, help='set if the TSV is a ROBOT template')
def tsvs2model(tsvfiles, output, schema_name, **kwargs):
    """ Infer a model from multiple TSVs """
    ie = CsvDataImportEngine(**kwargs)
    schema_dict = ie.convert_multiple(tsvfiles, schema_name=schema_name)
    ys = yaml.dump(schema_dict, default_flow_style=False, sort_keys=False)
    if output:
        with open(output, 'w') as stream:
            stream.write(ys)
    else:
        print(ys)


@main.command()
@click.argument('yamlfile')
@click.option('--zooma-confidence', '-Z', help='zooma confidence')
@click.option('--results', '-r', help='mapping results file')
def enrich(yamlfile, results, **args):
    """ Infer a model from a TSV """
    yamlobj = yaml.load(open(yamlfile))
    cache = {}
    infer_enum_meanings(yamlobj, cache=cache)
    if results is not None:
        with open(results, "w") as io:
            #io.write(str(cache))
            io.write(yaml.dump(cache))
    print(yaml.dump(yamlobj, default_flow_style=False, sort_keys=False))


if __name__ == '__main__':
    main()
