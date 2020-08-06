import os
import copy
import numpy as np
import subprocess
import itertools
from pathlib import Path
from bondnet.core.molwrapper import create_rdkit_mol_from_mol_graph
from bondnet.analysis.utils import TexWriter
from bondnet.utils import pickle_dump, pickle_load, expand_path


def check_connectivity_mol(
    mol,
    allowed_charge={
        "H": [1],
        "C": [1, 2, 3, 4],
        "O": [1, 2],
        "F": [1],
        "P": [1, 2, 3, 5, 6],  # 6 for LiPF6
        "N": [1, 2, 3, 4, 5],
        "S": [1, 2, 3, 4, 5, 6],
        # metal
        "Li": [1, 2, 3],
        "Mg": [1, 2, 3],
    },
    metal="Li",
):
    """
    Check the connectivity of each atom in a mol, without considering their bonding to
    metal element (e.g. Li), which forms coordinate bond with other atoms.
    """

    def get_neighbor_species(m):
        """
        Returns:
            A list of tuple (atom species, bonded atom species),
            where `bonded_atom_species` is a list.
            Each tuple represents an atom and its bonds.
        """
        res = [(s, []) for s in m.species]
        for (a1, a2), _ in m.bonds.items():
            s1 = m.species[a1]
            s2 = m.species[a2]
            res[a1][1].append(s2)
            res[a2][1].append(s1)
        return res

    neigh_species = get_neighbor_species(mol)

    do_fail = False
    reason = []

    for a_s, n_s in neigh_species:

        if len(n_s) == 0 and len(neigh_species) == 1:
            print("#####INFO##### single atom molecule:", mol.id)

        removed_metal = [s for s in n_s if s != metal]
        num_bonds = len(removed_metal)

        if num_bonds == 0:  # fine since we removed metal coordinate bonds
            continue

        if num_bonds not in allowed_charge[a_s]:
            reason.append("{} {}".format(a_s, num_bonds))
            do_fail = True

    return do_fail, reason


def check_bond_species_mol(mol, not_allowed=[("Li", "H"), ("Li", "Li")]):
    """
    Check the species of atoms associated with a bond.
    Bonds provided in `not_allowed` fail the check.
    """

    def get_bond_species(m):
        """
        Returns:
            A list of the two species associated with each bonds in the molecule.
        """
        res = []
        for (a1, a2), _ in m.bonds.items():
            s1 = m.species[a1]
            s2 = m.species[a2]
            res.append(sorted([s1, s2]))
        return res

    not_allowed = [sorted(i) for i in not_allowed]

    bond_species = get_bond_species(mol)

    do_fail = False
    reason = []
    for b in bond_species:
        if b in not_allowed:
            reason.append(str(b))
            do_fail = True

    return do_fail, reason


def check_bond_length_mol(mol):
    """
    Check the length of bonds. If larger than allowed length, it fails.

    """

    def get_bond_lengths(m):
        """
        Returns:
            A list of tuple (species, length), where species are the two species
            associated with a bond and length is the corresponding bond length.
        """
        res = []
        for (a1, a2), _ in m.bonds.items():
            s1 = m.species[a1]
            s2 = m.species[a2]
            c1 = np.asarray(m.coords[a1])
            c2 = np.asarray(m.coords[a2])
            length = np.linalg.norm(c1 - c2)
            res.append((tuple(sorted([s1, s2])), length))
        return res

    #
    # bond lengths references:
    # http://chemistry-reference.com/tables/Bond%20Lengths%20and%20Enthalpies.pdf
    # page 29 https://slideplayer.com/slide/17256509/
    # https://chem.libretexts.org/Bookshelves/Physical_and_Theoretical_Chemistry_Textbook_Maps/Supplemental_Modules_(Physical_and_Theoretical_Chemistry)/Chemical_Bonding/Fundamentals_of_Chemical_Bonding/Chemical_Bonds/Bond_Lengths_and_Energies
    #
    # unit: Angstrom
    #

    li_len = 2.8
    mg_len = 2.8
    bond_length_limit = {
        # H
        # ("H", "H"): 0.74,
        ("H", "H"): None,
        ("H", "C"): 1.09,
        ("H", "O"): 0.96,
        # ("H", "F"): 0.92,
        ("H", "F"): None,
        ("H", "P"): 1.44,
        ("H", "N"): 1.01,
        ("H", "S"): 1.34,
        ("H", "Li"): li_len,
        ("H", "Mg"): mg_len,
        # C
        ("C", "C"): 1.54,
        ("C", "O"): 1.43,
        ("C", "F"): 1.35,
        ("C", "P"): 1.84,
        ("C", "N"): 1.47,
        ("C", "S"): 1.81,
        ("C", "Li"): li_len,
        ("C", "Mg"): mg_len,
        # O
        ("O", "O"): 1.48,
        ("O", "F"): 1.42,
        ("O", "P"): 1.63,
        ("O", "N"): 1.44,
        ("O", "S"): 1.51,
        ("O", "Li"): li_len,
        ("O", "Mg"): mg_len,
        # F
        # ("F", "F"): 1.42,
        ("F", "F"): None,
        ("F", "P"): 1.54,
        ("F", "N"): 1.39,
        ("F", "S"): 1.58,
        ("F", "Li"): li_len,
        ("F", "Mg"): mg_len,
        # P
        ("P", "P"): 2.21,
        ("P", "N"): 1.77,
        ("P", "S"): 2.1,
        ("P", "Li"): li_len,
        ("P", "Mg"): mg_len,
        # N
        ("N", "N"): 1.46,
        ("N", "S"): 1.68,
        ("N", "Li"): li_len,
        ("N", "Mg"): mg_len,
        # S
        ("S", "S"): 2.04,
        ("S", "Li"): li_len,
        ("S", "Mg"): mg_len,
        # Li
        ("Li", "Li"): li_len,
        # Mg
        ("Mg", "Mg"): mg_len,
    }

    # multiply by 1.2 to relax the rule a bit
    tmp = dict()
    for k, v in bond_length_limit.items():
        if v is not None:
            v *= 1.2
        tmp[tuple(sorted(k))] = v
    bond_length_limit = tmp

    do_fail = False
    reason = []

    bond_species = get_bond_lengths(mol)
    for b, length in bond_species:
        limit = bond_length_limit[b]
        if limit is not None and length > limit:
            reason.append("{}  {} ({})".format(b, length, limit))
            do_fail = True

    return do_fail, reason


def check_connectivity(
    mols, metal, filename_failed="failed_check_connectivity.pkl",
):

    succeeded = []
    failed = []
    reason = []

    for m in mols:
        do_fail, rsn = check_connectivity_mol(m, metal=metal)
        if do_fail:
            failed.append(m)
            reason.append(rsn)
        else:
            succeeded.append(m)

    print("#" * 80)
    print("### Failed `check_connectivity()`")
    print("### number of entries failed:", len(failed))
    print("idx    id    atom specie    num bonds (without considering Li)")
    for i, (m, r) in enumerate(zip(failed, reason)):
        print(i, m.id, r)

    pickle_dump(failed, filename_failed)

    return succeeded


def check_rdkit_sanitize(
    mols, filename_failed="failed_check_rdkit_sanitize.pkl",
):

    succeeded = []
    failed = []
    reason = []

    for m in mols:
        try:
            create_rdkit_mol_from_mol_graph(m.mol_graph, force_sanitize=True)
            succeeded.append(m)
        except Exception as e:
            failed.append(m)
            reason.append(str(e))

    print("#" * 80)
    print("### Failed `check_rdkit_sanitize()`")
    print("### number of entries failed:", len(failed))
    print("idx    id    failing_reason")
    for i, (m, r) in enumerate(zip(failed, reason)):
        print(i, m.id, r)

    pickle_dump(failed, filename_failed)

    return succeeded


def check_bond_species(
    mols,
    not_allowed=[("Li", "H"), ("Li", "Li"), ("Mg", "Mg"), ("H", "Mg")],
    filename_failed="failed_check_bond_species.pkl",
):

    succeeded = []
    failed = []
    reason = []

    for m in mols:
        do_fail, rsn = check_bond_species_mol(m, not_allowed)
        if do_fail:
            failed.append(m)
            reason.append(rsn)
        else:
            succeeded.append(m)

    print("#" * 80)
    print("### Failed `check_bond_species()`")
    print("### number of entries failed:", len(failed))
    print("index    id     reason")
    for i, (m, r) in enumerate(zip(failed, reason)):
        print(i, m.id, r)

    pickle_dump(failed, filename_failed)

    return succeeded


def check_bond_length(mols, filename_failed="failed_check_bond_length.pkl"):

    succeeded = []
    failed = []
    reason = []

    for m in mols:
        do_fail, rsn = check_bond_length_mol(m)
        if do_fail:
            failed.append(m)
            reason.append(rsn)
        else:
            succeeded.append(m)

    print("#" * 80)
    print("### Failed `check_bond_length()`")
    print("### number of entries failed:", len(failed))
    print("index    id     bond     length (limit)")
    for i, (m, r) in enumerate(zip(failed, reason)):
        print(i, m.id, r)

    pickle_dump(failed, filename_failed)

    return succeeded


def remove_mols_containing_species(
    mols, species, filename_failed="failed_remove_mols_containing_species.pkl",
):
    """
    Remove molecules containing the given species.
    """

    def check_one(m, species):
        for s in species:
            if s in m.species:
                return True, s
        return False, None

    succeeded = []
    failed = []
    reason = []

    for m in mols:
        do_fail, rsn = check_one(m, species)
        if do_fail:
            failed.append(m)
            reason.append(rsn)
        else:
            succeeded.append(m)

    print("#" * 80)
    print("### Failed `remove_mols_containing_species()`")
    print("### number of entries failed:", len(failed))
    print("index    id     species")
    for i, (m, r) in enumerate(zip(failed, reason)):
        print(i, m.id, r)

    pickle_dump(failed, filename_failed)

    return succeeded


def check_all(
    filename="~/Applications/db_access/mol_builder/molecules_n200.pkl",
    output_prefix=None,
):

    mols = pickle_load(filename)
    print("Number of mols before any check:", len(mols))

    if output_prefix is None:
        output_prefix = Path(filename).parent

    mols = check_connectivity(
        mols=mols,
        metal="Li",
        filename_failed=output_prefix.joinpath("failed_connectivity.pkl"),
    )
    mols = check_rdkit_sanitize(
        mols=mols, filename_failed=output_prefix.joinpath("failed_rdkit_sanitize.pkl")
    )
    mols = check_bond_species(
        mols=mols, filename_failed=output_prefix.joinpath("failed_bond_species.pkl")
    )
    mols = check_bond_length(
        mols=mols, filename_failed=output_prefix.joinpath("failed_bond length.pkl")
    )
    mols = remove_mols_containing_species(
        mols=mols,
        species=["P"],
        filename_failed=output_prefix.joinpath("failed_containing_species.pkl"),
    )

    print("Number of mols after check:", len(mols))

    outname = output_prefix.joinpath(Path(filename).stem + "_qc" + Path(filename).suffix)
    pickle_dump(mols, outname)


def plot_mol_graph(
    filename="~/Applications/db_access/mol_builder/molecules.pkl",
    # filename="~/Applications/db_access/mol_builder/molecules_n200.pkl",
):
    def plot_one(m, prefix):
        fname = os.path.join(prefix, "{}.png".format(m.id))
        fname = expand_path(fname)
        m.draw(fname, show_atom_idx=True)
        subprocess.run(["convert", fname, "-trim", "-resize", "100%", fname])

    mols = pickle_load(filename)
    for m in mols:

        # # mol builder
        # prefix = "~/Applications/db_access/mol_builder/png_union_builder"
        # plot_one(m, prefix)

        # babel builder with extender
        m.convert_to_babel_mol_graph(use_metal_edge_extender=False)
        prefix = "~/Applications/db_access/mol_builder/png_babel_builder"
        plot_one(m, prefix)

        # # babel builder with extender
        # m.convert_to_babel_mol_graph(use_metal_edge_extender=True)
        # prefix = "~/Applications/db_access/mol_builder/png_extender_builder"
        # plot_one(m, prefix)
        #
        # # critic
        # m.convert_to_critic_mol_graph()
        # prefix = "~/Applications/db_access/mol_builder/png_critic_builder"
        # plot_one(m, prefix)


def compare_connectivity_across_graph_builder(
    filename="~/Applications/db_access/mol_builder/molecules.pkl",
    # filename="~/Applications/db_access/mol_builder/molecules_n200.pkl",
    tex_file="~/Applications/db_access/mol_builder/tex_mol_connectivity_comparison.tex",
    only_different=True,
    checker=[
        check_connectivity_mol,
        check_rdkit_sanitize,
        check_bond_length_mol,
        check_bond_species_mol,
    ],
):
    """
    Plot the mol connectivity and see how different they are.
    """

    molecules = pickle_load(filename)

    # ###############
    # # filter on charge
    # ###############
    # new_mols = []
    # for m in mols:
    #     if m.charge == 0:
    #         new_mols.append(m)
    # mols = new_mols

    # keep record of molecules of which the babel mol graph and critic mol graph are
    # different
    mols_differ_graph = []
    for m in molecules[:1500]:

        # mol builder
        m1 = copy.deepcopy(m)

        # babel builder
        m.convert_to_babel_mol_graph(use_metal_edge_extender=False)
        m2 = copy.deepcopy(m)

        # babel builder with extender
        m.convert_to_babel_mol_graph(use_metal_edge_extender=True)
        m3 = copy.deepcopy(m)

        # critic
        m.convert_to_critic_mol_graph()
        m4 = copy.deepcopy(m)

        if not only_different or not m3.mol_graph.isomorphic_to(m4.mol_graph):
            mols_differ_graph.append([m1, m2, m3, m4])

    # filter out (remove) molecules where both the babel graph and critic graph fail the
    # same checker
    if checker is not None:
        remaining = []
        reason = []

        for m1, m2, m3, m4 in mols_differ_graph:

            fail_both_check = False
            rsn = []

            for ck in checker:
                # we only check whether babel extender and critic fails
                fail3, rsn3 = ck(m3)
                fail4, rsn4 = ck(m4)
                if fail3 and fail4:
                    fail_both_check = True
                    break
                else:
                    rsn.append([rsn3, rsn4])

            # do not take a look for ones that fail both
            if fail_both_check:
                continue
            else:
                remaining.append([m1, m2, m3, m4])
                reason.append(rsn)

        mols_differ_graph = remaining

    # write tex file
    tex_file = expand_path(tex_file)
    with open(tex_file, "w") as f:
        f.write(TexWriter.head())
        f.write(
            "On each page, we plot 4 mols (top to bottom) from: the union of metal "
            "extender and critic, babel without extender, babel with extender and the "
            "critic builder.\n"
        )

        for i, mols in enumerate(mols_differ_graph):
            m = mols[0]

            # molecule info
            f.write(TexWriter.newpage())
            f.write("formula: " + m.formula + "\n\n")
            f.write("charge: " + str(m.charge) + "\n\n")
            f.write("spin multiplicity: " + str(m.spin_multiplicity) + "\n\n")
            f.write("free energy: " + str(m.free_energy) + "\n\n")
            f.write("id: " + m.id + "\n\n")

            # edge distances
            f.write("atom pair distances:\n\n")

            for a1, a2 in itertools.combinations(range(m.num_atoms), 2):
                dist = np.linalg.norm(m.coords[a1] - m.coords[a2])
                f.write("{} {}: {:.3f}\n\n".format(a1 + 1, a2 + 1, dist))

            # comparing edge differences between builder
            babel_bonds = set([(a1 + 1, a2 + 1) for (a1, a2), _ in mols[1].bonds.items()])
            extender_bonds = set(
                [(a1 + 1, a2 + 1) for (a1, a2), _ in mols[2].bonds.items()]
            )
            critic_bonds = set(
                [(a1 + 1, a2 + 1) for (a1, a2), _ in mols[3].bonds.items()]
            )

            intersection = extender_bonds.intersection(critic_bonds)
            extender_not_in_critic = extender_bonds - intersection
            critic_not_in_extender = critic_bonds - intersection

            # # babel missing Li bond (do not include)
            # missing_li_bond = True
            # for b in critic_not_in_extender:
            #     if m.species[b[0]] != "Li" and m.species[b[1]] != "Li":
            #         missing_li_bond = False
            #         break
            # if missing_li_bond:
            #     continue
            #
            # # critic bond to itself
            # self_bond = False
            # for b in critic_bonds:
            #     if b[0] == b[1]:
            #         self_bond = True
            #         break
            # if self_bond:
            #     continue

            #################
            #################
            f.write("extender added to babel: ")
            for b in extender_bonds - babel_bonds:
                f.write("{} ".format(b))
            f.write("\n\n")
            f.write("extender bond not in critic: ")
            for b in extender_not_in_critic:
                f.write("{} ".format(b))
            f.write("\n\n")
            f.write("critic bond not in extender: ")
            for b in critic_not_in_extender:
                f.write("{} ".format(b))
            f.write("\n\n")

            # add mol graph png
            for j, m in enumerate(mols):
                if j == 0:
                    p = "png_union_builder"
                elif j == 1:
                    p = "png_babel_builder"
                elif j == 2:
                    p = "png_extender_builder"
                elif j == 3:
                    p = "png_critic_builder"
                fname = os.path.join(
                    "~/Applications/db_access/mol_builder", p, "{}.png".format(m.id)
                )
                fname = expand_path(fname)

                f.write(TexWriter.single_figure(fname, figure_size=0.2))
                f.write(TexWriter.verbatim("=" * 80))

            # if use checker, write reason
            if checker is not None:
                f.write(TexWriter.verbatim(TexWriter.resize_string(str(reason[i]))))

        f.write(TexWriter.tail())

    filename = "~/Applications/db_access/mol_builder/molecules_union_builder.pkl"
    mols = [i[0] for i in mols_differ_graph]
    pickle_dump(mols, filename)
    filename = "~/Applications/db_access/mol_builder/molecules_babel_builder.pkl"
    mols = [i[1] for i in mols_differ_graph]
    pickle_dump(mols, filename)
    filename = "~/Applications/db_access/mol_builder/molecules_extender_builder.pkl"
    mols = [i[2] for i in mols_differ_graph]
    pickle_dump(mols, filename)
    filename = "~/Applications/db_access/mol_builder/molecules_critic_builder.pkl"
    mols = [i[3] for i in mols_differ_graph]
    pickle_dump(mols, filename)

    print(
        "### mol graph comparison. number of mols {}, different mol graphs by "
        "babel extender builder and critic builder: {}".format(
            len(molecules), len(mols_differ_graph)
        )
    )


if __name__ == "__main__":

    filename = "~/Applications/db_access/mol_builder/molecules.pkl"
    check_all(filename)

    # plot_mol_graph()
    # compare_connectivity_across_graph_builder()