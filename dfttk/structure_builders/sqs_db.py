"""
The sqs_db module handles creating and interacting with an SQS database of pymatgen-serialized SQS.

There are helper functions for converting SQS generated by mcsqs in ATAT to pymatgen Structure
objects that  serialization.

The sublattice and types are the same as used in ATAT, where `a_B` corresponds to atom `B` in
sublattice `a`. Due to the way dummy species are implemented in pymatgen, these species in pymatgen
Structures are renamed to `Xab`, which similarly corresponds to atom `B` in sublattice `a`.

In general, the workflow to create a database is to use the helper functions to
1. convert the lattice.in ATAT files to CIFs with renaming
2. create Structure objects from those CIFs, removing oxidation states (no helper function)
3. write those Structures to the database
4. persist the database to a path (no helper function)

Later, the database can be constructed again from the path, added to (steps 1-3) and persisted again.

To use the database, the user calls the `structures_from_database` helper function to generate a list
of all the SQS that match the endmember symmetry, sublattice model (and site ratios) that define a
phase. This is intentionally designed to match the syntax used to describe phases in ESPEI. Each of
the resulting Structure objects can be made concrete using functions in `dfttk.sqs`.
"""

import json
import os
import re

from pymatgen import Lattice
from pyparsing import Regex, Word, alphas, alphanums, OneOrMore, LineEnd, Suppress, Group
from tinydb import TinyDB
from tinydb.storages import MemoryStorage

from dfttk.structure_builders.sqs import AbstractSQS
from dfttk.utils import recursive_glob

def _parse_atat_lattice(lattice_in):
    """Parse an ATAT-style `lat.in` string.

    The parsed string will be in three groups: (Coordinate system) (lattice) (atoms)
    where the atom group is split up into subgroups, each describing the position and atom name
    """
    float_number = Regex(r'[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?').setParseAction(lambda t: [float(t[0])])
    vector = Group(float_number + float_number + float_number)
    angles = vector
    vector_line = vector + Suppress(LineEnd())
    coord_sys = Group((vector_line + vector_line + vector_line) | (vector + angles + Suppress(LineEnd())))
    lattice = Group(vector + vector + vector)
    atom = Group(vector + Group(OneOrMore(Word(alphas, alphanums + '_'))))
    atat_lattice_grammer = coord_sys + lattice + Group(OneOrMore(atom))
    # parse the input string and convert it to a POSCAR string
    return atat_lattice_grammer.parseString(lattice_in)

def lat_in_to_sqs(atat_lattice_in, rename=True):
    """
    Convert a string-like ATAT-style lattice.in to an abstract SQS.

    Parameters
    ----------
    atat_lattice_in : str
        String-like of a lattice.in in the ATAT format.
    rename : bool
        If True, SQS format element names will be renamed, e.g. `a_B` -> `Xab`. Default is True.

    Returns
    -------
    SQS
        Abstract SQS.
    """
    # TODO: handle numeric species, e.g. 'g1'. Fixed
    # Problems: parser has trouble with matching next line and we have to rename it so pymatgen
    # doesn't think it's a charge.
    # parse the data
    parsed_data = _parse_atat_lattice(atat_lattice_in)
    atat_coord_system = parsed_data[0]
    atat_lattice = parsed_data[1]
    atat_atoms = parsed_data[2]
    # create the lattice
    if len(atat_coord_system) == 3:
        # we have a coordinate system matrix
        coord_system = Lattice(list(atat_coord_system)).matrix
    else:
        # we have length and angles
        #coord_system = Lattice.from_lengths_and_angles(list(atat_coord_system[0]), list(atat_coord_system[1])).matrix
        (lat_a, lat_b, lat_c) = list(atat_coord_system[0])
        (lat_alpha, lat_beta, lat_gamma) = list(atat_coord_system[1])
        coord_system = Lattice.from_parameters(lat_a, lat_b, lat_c, lat_alpha, lat_beta, lat_gamma).matrix
    direct_lattice = Lattice(list(atat_lattice))
    lattice = coord_system.dot(direct_lattice.matrix)
    # create the list of atoms, converted to the right coordinate system
    species_list = []
    species_positions = []
    subl_model = {} # format {'subl_name': 'atoms_found_in_subl, e.g. "aaabbbb"'}
    for position, atoms in atat_atoms:
        # atoms can be a list of atoms, e.g. for not abstract SQS
        if len(atoms) > 1:
            raise NotImplementedError('Cannot parse atom list {} because the sublattice is unclear.\nParsed data: {}'.format(atoms, atat_atoms))
        atom = atoms[0]
        if rename:
            # change from `a_B` style to `Xab`

            atom = atom.lower().split('_')
        else:
            raise NotImplementedError('Cannot rename because the atom name and sublattice name may be ambigous.')
        # add the abstract atom to the sublattice model
        subl = atom[0]
        #Replace the digital by alphas, 1->a, 2->b, 3->c, ...
        rep_items = re.findall("\d+", subl)
        for rep_item in rep_items:
            subl = subl.replace(rep_item, chr(96 + int(rep_item)))
        subl_atom = atom[1]
        subl_model[subl] = subl_model.get(subl, set()).union({subl_atom})
        # add the species and position to the lists
        species_list.append('X'+subl+subl_atom)
        species_positions.append(list(position))
    # create the structure
    sublattice_model = [[e for e in sorted(list(set(subl_model[s])))] for s in sorted(subl_model.keys())]
    sublattice_names = [s for s in sorted(subl_model.keys())]
    sqs = AbstractSQS(direct_lattice, species_list, species_positions, coords_are_cartesian=True,
              sublattice_model=sublattice_model,
              sublattice_names=sublattice_names)
    sqs.lattice = Lattice(lattice)
    #sqs.modify_lattice(Lattice(lattice))  #This will be deprecated in v2020

    return sqs


def SQSDatabase(path, name_constraint=''):
    """
    Convienence function to create a TinyDB for the SQS database found at `path`.

    Parameters
    ----------
    path : path-like of the folder containing the SQS database.
    name_constraint : Any name constraint to add into the recursive glob. Not case sensitive. Exact substring.

    Returns
    -------
    TinyDB
        Database of abstract SQS.
    """
    db = TinyDB(storage=MemoryStorage)
    dataset_filenames = recursive_glob(path, '*.json')
    dataset_filenames = [fname for fname in dataset_filenames if name_constraint.upper() in fname.upper()]
    for fname in dataset_filenames:
        with open(fname) as file_:
            try:
                db.insert(json.load(file_))
            except ValueError as e:
                raise ValueError('JSON Error in {}: {}'.format(fname, e))
    return db

def SQSDatabaseATAT(atat_sqsdb_path):
    """
    Generate the SQS database using the build-in sqsdb in ATAT
    """
    sqsfilename = "bestsqs.out"
    for diri in os.listdir(atat_sqsdb_path):
        sqsgen_path = os.path.join(atat_sqsdb_path, diri)
        if os.path.isdir(sqsgen_path):
            # prototype = [name_prototype, Strukturbericht_mark]
            prototype = diri.split("_")
            for atatsqs_path in os.listdir(sqsgen_path):
                sqs_path = os.path.join(sqsgen_path, atatsqs_path)
                if os.path.isdir(sqs_path):
                    #sqs_config = parse_atatsqs_path(atatsqs_path)
                    sqsfile_fullpath = os.path.join(sqs_path, sqsfilename)
                    if os.path.exists(sqsfile_fullpath):
                        with open(sqsfile_fullpath, "r") as fid:
                            #sqs_str = fid.read()
                            sqs_dict = lat_in_to_sqs(fid.read()).as_dict()
                            #print(sqs_dict)

def parse_atatsqs_path(atatsqs_path):
    """
    Parse the path of atat sqsdb

    Parameters
    ----------
        atatsqs_path: str
            The path of atat sqsdb, e.g. sqsdb_lev=2_a=0.5,0.5_f=0.5,0.5
    Return
    ------
        sqs_config: dict
            The dict of the configuration of sqs
            It contains the following keys:
                level: The level of sqs, usually 0 for pure elements, 1 for 50%-50%, ...
                configuration: The configuration of sqs, e.g. ['a', 'c']
                occupancies: The occupancies of sqs, e.g. [0.5, 0.5]
    """
    sqs_config = {}
    configuration = []
    occupancies = []
    path_list = atatsqs_path.split("_")
    for path_param in path_list:
        path_val = path_param.split("=")
        if path_val[0] == "sqsdb":
            pass
        elif path_val[0] == "lev":
            level = path_val[1]
        else:
            configuration.append(path_val[0])
            occupancies.append(path_val[1].split(","))
    sqs_config["level"] = level
    sqs_config["configuration"] = configuration
    sqs_config["occupancies"] = occupancies
    return sqs_config


def read_sqsgen_in(sqsgen_path):
    """
    Read sqsgen.in file in the ATAT's SQS database

    Parameters
    ----------
        sqsgen_path: str 
            The path of sqsgen.in
    Returns
    -------
        sqs_folder: str
            The folder name of bestsqs.out file
        sqs_config: dict
            The dict of the configuration of sqs
            It contains the following keys:
                n: The number of ith sqs structure
            The value is a dict too, it has following keys:
                level: The level of sqs, usually 0 for pure elements, 1 for 50%-50%, ...
                configuration: The configuration of sqs, e.g. ['a', 'c']
                occupancies: The occupancies of sqs, e.g. [0.5, 0.5]
    """
    f_count = 0
    sqs_folders = []
    sqs_config = {}
    with open(os.path.join(sqsgen_path, "sqsgen.in"), "r+") as fid:
        for eachline in fid:
            eachline = re.split('\s+', eachline.strip("\n"))
            configuration = []
            occupancies = []
            for linei in eachline:
                line_list = linei.split("=")
                if line_list[0] == "level":
                    leveli = line_list[1]
                else:
                    configuration.append(line_list[0])
                    occupancies.append(line_list[1].split(","))
            sqs_config[f_count] = {}
            sqs_config[f_count]["level"] = leveli
            sqs_config[f_count]["configuration"] = configuration
            sqs_config[f_count]["occupancies"] = occupancies
            sqs_folder = "_".join(eachline)
            sqs_folders.append("sqsdb_" + sqs_folder)
            f_count += 1
    return sqs_folders, sqs_config

def get_structures_from_database(db, symmetry, subl_model, subl_site_ratios):
    """Returns a list of Structure objects from the db that match the criteria.

    The returned list format supports matching SQS to phases that have multiple solution sublattices
    and the inclusion of higher and lower ordered SQS that match the criteria.

    Parameters
    ----------
    db : tinydb.database.Table
        TinyDB database of the SQS database
    symmetry : str
        Spacegroup symbol for a non-mixing endmember as in pymatgen, e.g. 'Pm-3m'.
    subl_model : [[str]]
        List of strings of species names. This sublattice model can be of higher dimension than the SQS.
        Outer dimension should be the same length as subl_site_ratios.
    subl_site_ratios : [[float]]
        Scalar multiple of site ratios of each sublattice. e.g. [1, 2] will match [2, 4] and vice
        versa. Outer dimension should be the same length as subl_model.

    Returns
    -------
    [AbstractSQS]
        Abstract SQSs that match the symmetry and sublattice model.
    """
    def lists_are_multiple(l1, l2):
        """
        Returns True if list a is a multiple of b or vice versa

        Parameters
        ----------
        l1 : [int]
        l2 : [int]

        Returns
        -------
        bool

        """
        # can we compare these two lists?
        if len(l1) != len(l2):
            return False
        for a, b in [(l1, l2), (l2, l1)]:  # check the reverse of the lists too
            # see if all a are perfectly divisible by all b
            if all([(x % y == 0) for x, y in zip(a, b)]):
                # see if all have the same multiple
                if len(set([x/y for x, y in zip(a, b)])) == 1:
                    return True
        return False

    from tinydb import where
    results = db.search((where('symmetry').symbol == symmetry) &
                        (where('sublattice_site_ratios').test(
                         lambda x: (lists_are_multiple([sum(subl) for subl in x], subl_site_ratios))))
              )
    return results
