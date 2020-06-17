import sys
import time
import warnings
import torch
import argparse
import numpy as np
from datetime import datetime
from itertools import compress
from torch import autograd
from torch.optim.lr_scheduler import ReduceLROnPlateau
from gnn.training_script.metric import (
    WeightedMSELoss,
    WeightedL1Loss,
    EarlyStopping,
    OrderAccuracy,
)
from gnn.model.hgat_bond import HGATBond
from gnn.data.dataset import train_validation_test_split_test_with_all_bonds_of_mol
from gnn.data.electrolyte import ElectrolyteBondDataset
from gnn.data.dataloader import DataLoaderBond
from gnn.data.grapher import HeteroMoleculeGraph
from gnn.data.featurizer import (
    AtomFeaturizer,
    BondAsNodeFeaturizer,
    GlobalFeaturizerCharge,
)
from gnn.utils import pickle_dump, seed_torch, load_checkpoints


def parse_args():
    parser = argparse.ArgumentParser(description="HGATBond")

    # model
    parser.add_argument(
        "--num-gat-layers", type=int, default=3, help="number of GAT layers"
    )
    parser.add_argument(
        "--gat-hidden-size",
        type=int,
        nargs="+",
        default=[32, 32, 64],
        help="number of hidden units of GAT layers",
    )
    parser.add_argument(
        "--num-heads", type=int, default=1, help="number of hidden attention heads"
    )
    parser.add_argument(
        "--feat-drop", type=float, default=0.0, help="input feature dropout"
    )
    parser.add_argument("--attn-drop", type=float, default=0.0, help="attention dropout")
    parser.add_argument(
        "--negative-slope",
        type=float,
        default=0.2,
        help="the negative slope of leaky relu",
    )

    parser.add_argument(
        "--gat-num-fc-layers",
        type=int,
        default=3,
        help="number of fc layers in gat node attantion layer",
    )

    parser.add_argument(
        "--gat-residual", type=int, default=1, help="residual connection for gat layer"
    )

    parser.add_argument(
        "--gat-batch-norm", type=int, default=0, help="batch norm for gat layer"
    )

    parser.add_argument(
        "--gat-activation", type=str, default="ELU", help="activation fn for gat layer"
    )

    parser.add_argument(
        "--readout-type", type=str, default="bond", help="type of readout bond feature"
    )

    parser.add_argument(
        "--num-fc-layers", type=int, default=3, help="number of feed-forward layers"
    )
    parser.add_argument(
        "--fc-hidden-size",
        type=int,
        nargs="+",
        default=[128, 64, 32],
        help="number of hidden units of fc layers",
    )
    parser.add_argument(
        "--fc-batch-norm", type=int, default=0, help="batch nonrm for fc layer"
    )
    parser.add_argument(
        "--fc-activation", type=str, default="ELU", help="activation fn for fc layer"
    )
    parser.add_argument(
        "--fc-drop", type=float, default=0.0, help="dropout rato for fc layer"
    )

    # training
    parser.add_argument("--gpu", type=int, default=-1, help="GPU index. -1 to use CPU.")
    parser.add_argument("--epochs", type=int, default=1000, help="number of epochs")
    parser.add_argument("--batch-size", type=int, default=100, help="batch size")
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
    parser.add_argument("--weight-decay", type=float, default=0.0, help="weight decay")

    # output file (needed by hypertunity)
    parser.add_argument(
        "--output_file", type=str, default="results.pkl", help="name of output file"
    )

    parser.add_argument("--restore", type=int, default=0, help="read checkpoints")

    args = parser.parse_args()

    if args.gpu >= 0 and torch.cuda.is_available():
        args.device = torch.device("cuda:{}".format(args.gpu))
    else:
        args.device = None

    if len(args.gat_hidden_size) == 1:
        args.gat_hidden_size = args.gat_hidden_size * args.num_gat_layers
    else:
        assert len(args.gat_hidden_size) == args.num_gat_layers, (
            "length of `gat-hidden-size` should be equal to `num-gat-layers`, but got "
            "{} and {}.".format(args.gat_hidden_size, args.num_gat_layers)
        )

    if len(args.fc_hidden_size) == 1:
        args.fc_hidden_size = args.fc_hidden_size * args.num_fc_layers
    else:
        assert len(args.fc_hidden_size) == args.num_fc_layers, (
            "length of `fc-hidden-size` should be equal to `num-fc-layers`, but got "
            "{} and {}.".format(args.fc_hidden_size, args.num_fc_layers)
        )

    # if len(args.gat_hidden_size) == 1:
    #     val = args.gat_hidden_size[0]
    #     args.gat_hidden_size = [val * 2 ** i for i in range(args.num_gat_layers)]
    # else:
    #     assert len(args.gat_hidden_size) == args.num_gat_layers, (
    #         "length of `gat-hidden-size` should be equal to `num-gat-layers`, but got "
    #         "{} and {}.".format(args.gat_hidden_size, args.num_gat_layers)
    #     )

    # if len(args.fc_hidden_size) == 1:
    #     val = args.fc_hidden_size[0]
    #     args.fc_hidden_size = [val // 2 ** i for i in range(args.num_fc_layers)]
    # else:
    #     assert len(args.fc_hidden_size) == args.num_fc_layers, (
    #         "length of `fc-hidden-size` should be equal to `num-fc-layers`, but got "
    #         "{} and {}.".format(args.fc_hidden_size, args.num_fc_layers)
    #     )

    return args


def train(optimizer, model, nodes, data_loader, loss_fn, metric_fn, device=None):
    """
    Args:
        metric_fn (function): the function should be using a `sum` reduction method.
    """

    model.train()

    epoch_loss = 0.0
    accuracy = 0.0
    count = 0.0

    for it, (bg, label) in enumerate(data_loader):
        feats = {nt: bg.nodes[nt].data["feat"] for nt in nodes}
        label_val = label["value"]
        label_ind = label["indicator"]
        try:
            stdev = label["scaler_stdev"]
        except KeyError:
            stdev = None

        if device is not None:
            feats = {k: v.to(device) for k, v in feats.items()}
            label_val = label_val.to(device)
            label_ind = label_ind.to(device)
            if stdev is not None:
                stdev = stdev.to(device)

        pred = model(bg, feats)
        loss = loss_fn(pred, label_val, label_ind)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        epoch_loss += loss.detach().item()
        weight = label_ind if stdev is None else label_ind * stdev
        accuracy += metric_fn(pred, label_val, weight).detach().item()
        count += sum(label_ind).item()

    epoch_loss /= it + 1
    accuracy /= count

    return epoch_loss, accuracy


def evaluate(model, nodes, data_loader, metric_fn, device=None):
    """
    Evaluate the accuracy of an validation set of test set.

    Args:
        metric_fn (function): the function should be using a `sum` reduction method.
    """
    model.eval()

    with torch.no_grad():
        accuracy = 0.0
        count = 0.0

        for bg, label in data_loader:
            feats = {nt: bg.nodes[nt].data["feat"] for nt in nodes}
            label_val = label["value"]
            label_ind = label["indicator"]
            try:
                stdev = label["scaler_stdev"]
            except KeyError:
                stdev = None

            if device is not None:
                feats = {k: v.to(device) for k, v in feats.items()}
                label_val = label_val.to(device)
                label_ind = label_ind.to(device)
                if stdev is not None:
                    stdev = stdev.to(device)

            pred = model(bg, feats)

            weight = label_ind if stdev is None else label_ind * stdev
            accuracy += metric_fn(pred, label_val, weight).detach().item()
            count += sum(label_ind).item()

    return accuracy / count


def ordering_accuracy(model, nodes, data_loader, device=None):
    """
    Evaluate the accuracy of an validation set of test set.
    """

    model.eval()

    all_pred = []
    all_target = []
    all_ind = []
    all_mol_source = []

    with torch.no_grad():

        for bg, label in data_loader:
            feats = {nt: bg.nodes[nt].data["feat"] for nt in nodes}
            label_target = label["value"]
            label_ind = label["indicator"]
            label_id = label["id"]
            label_size = label["size"]

            if device is not None:
                feats = {k: v.to(device) for k, v in feats.items()}

            pred = model(bg, feats)

            # each element of these list corresponds to a bond

            all_pred.extend(
                [t.detach().numpy() for t in torch.split(pred, label_size)]
            )  # list of 1D array

            all_target.extend(
                [t.detach().numpy() for t in torch.split(label_target, label_size)]
            )  # list of 1D array

            all_ind.extend(
                [t.detach().numpy() for t in torch.split(label_ind, label_size)]
            )  # list of 1D array

            all_mol_source.extend(label_id)  # list of str

    # select the bond that has energy
    all_pred = np.asarray(
        [list(compress(v, i)) for v, i in zip(all_pred, all_ind)]
    ).reshape(-1)
    all_target = np.asarray(
        [list(compress(v, i)) for v, i in zip(all_target, all_ind)]
    ).reshape(-1)

    oa = OrderAccuracy(max_n=3)
    return oa.step(all_pred, all_target, all_mol_source)


def get_grapher():
    atom_featurizer = AtomFeaturizer()
    bond_featurizer = BondAsNodeFeaturizer(length_featurizer="bin")
    global_featurizer = GlobalFeaturizerCharge()
    grapher = HeteroMoleculeGraph(
        atom_featurizer=atom_featurizer,
        bond_featurizer=bond_featurizer,
        global_featurizer=global_featurizer,
        self_loop=True,
    )
    return grapher


def main(args):
    print("\n\nStart training at:", datetime.now())

    ### dataset
    # sdf_file = "~/Applications/db_access/mol_builder/struct_n200.sdf"
    # label_file = "~/Applications/db_access/mol_builder/label_n200.txt"
    # feature_file = "~/Applications/db_access/mol_builder/feature_n200.yaml"
    sdf_file = "~/Applications/db_access/mol_builder/zinc_struct_bond_rgrn_n200.sdf"
    label_file = "~/Applications/db_access/mol_builder/zinc_label_bond_rgrn_n200.txt"
    feature_file = "~/Applications/db_access/mol_builder/zinc_feature_bond_rgrn_n200.yaml"
    dataset = ElectrolyteBondDataset(
        grapher=get_grapher(),
        sdf_file=sdf_file,
        label_file=label_file,
        feature_file=feature_file,
        feature_transformer=True,
        label_transformer=True,
    )

    trainset, valset, testset = train_validation_test_split_test_with_all_bonds_of_mol(
        dataset, validation=0.1, test=0.1
    )
    print(
        "Trainset size: {}, valset size: {}: testset size: {}.".format(
            len(trainset), len(valset), len(testset)
        )
    )

    train_loader = DataLoaderBond(trainset, batch_size=args.batch_size, shuffle=True)
    # larger val and test set batch_size is faster but needs more memory
    # adjust the batch size of to fit memory
    bs = max(len(valset) // 10, 1)
    val_loader = DataLoaderBond(valset, batch_size=bs, shuffle=False)
    bs = max(len(testset) // 10, 1)
    test_loader = DataLoaderBond(testset, batch_size=bs, shuffle=False)

    ### model
    attn_mechanism = {
        "atom": {"edges": ["b2a", "g2a", "a2a"], "nodes": ["bond", "global", "atom"]},
        "bond": {"edges": ["a2b", "g2b", "b2b"], "nodes": ["atom", "global", "bond"]},
        "global": {"edges": ["a2g", "b2g", "g2g"], "nodes": ["atom", "bond", "global"]},
    }
    attn_order = ["atom", "bond", "global"]

    # attn_mechanism = {
    #     "atom": {"edges": ["b2a", "a2a"], "nodes": ["bond", "atom"]},
    #     "bond": {"edges": ["a2b", "b2b"], "nodes": ["atom", "bond"]},
    # }
    # attn_order = ["atom", "bond"]

    in_feats = trainset.get_feature_size(attn_order)
    model = HGATBond(
        attn_mechanism,
        attn_order,
        in_feats,
        num_gat_layers=args.num_gat_layers,
        gat_hidden_size=args.gat_hidden_size,
        num_heads=args.num_heads,
        feat_drop=args.feat_drop,
        attn_drop=args.attn_drop,
        negative_slope=args.negative_slope,
        gat_num_fc_layers=args.gat_num_fc_layers,
        gat_residual=args.gat_residual,
        gat_batch_norm=args.gat_batch_norm,
        gat_activation=args.gat_activation,
        readout_type=args.readout_type,
        num_fc_layers=args.num_fc_layers,
        fc_hidden_size=args.fc_hidden_size,
        fc_batch_norm=args.fc_batch_norm,
        fc_activation=args.fc_activation,
        fc_drop=args.fc_drop,
        outdim=1,
    )
    print(model)

    if args.device is not None:
        model.to(device=args.device)

    ### optimizer, loss, and metric
    optimizer = torch.optim.Adam(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )

    loss_func = WeightedMSELoss(reduction="mean")
    metric = WeightedL1Loss(reduction="sum")

    ### learning rate scheduler and stopper
    scheduler = ReduceLROnPlateau(
        optimizer, mode="min", factor=0.4, patience=50, verbose=True
    )
    stopper = EarlyStopping(patience=150)

    checkpoints_objs = {"model": model, "optimizer": optimizer, "scheduler": scheduler}
    if args.restore:
        try:
            load_checkpoints(checkpoints_objs)
            print("Successfully load checkpoints")
        except FileNotFoundError as e:
            warnings.warn(str(e) + " Continue without loading checkpoints.")
            pass

    print(
        "\n\n# Epoch     Loss         TrainAcc        ValAcc        OrdAcc     Time ("
        "s)"
    )
    sys.stdout.flush()

    for epoch in range(args.epochs):
        ti = time.time()

        # train and evaluate accuracy
        loss, train_acc = train(
            optimizer, model, attn_order, train_loader, loss_func, metric, args.device
        )

        # bad, we get nan. Before existing, do some debugging
        if np.isnan(loss):
            print("\n\nBad, we get nan for loss. See below for traceback\n\n")
            sys.stdout.flush()
            with autograd.detect_anomaly():
                train(
                    optimizer,
                    model,
                    attn_order,
                    train_loader,
                    loss_func,
                    metric,
                    args.device,
                )
            sys.exit(1)

        val_acc = evaluate(model, attn_order, val_loader, metric, args.device)

        # note, we should use test_loader here since it contains all bond energies for
        # each molecule in it
        ordering_score = ordering_accuracy(model, attn_order, test_loader, args.device)

        if stopper.step(val_acc, checkpoints_objs, msg="epoch " + str(epoch)):
            # save results for hyperparam tune
            pickle_dump(float(stopper.best_score), args.output_file)
            break

        scheduler.step(val_acc)

        tt = time.time() - ti

        print(
            "{:5d}   {:12.6e}   {:12.6e}   {:12.6e}   {}   {:.2f}".format(
                epoch, loss, train_acc, val_acc, ordering_score, tt
            )
        )
        if epoch % 10 == 0:
            sys.stdout.flush()

    # save results for hyperparam tune
    pickle_dump(float(stopper.best_score), args.output_file)

    # load best to calculate test accuracy
    load_checkpoints(checkpoints_objs)
    test_acc = evaluate(model, attn_order, test_loader, metric, args.device)
    ordering_score = ordering_accuracy(model, attn_order, test_loader, args.device)

    print("\n#TestAcc: {:12.6e}\n".format(test_acc))
    print(f"\n#Test Order Accuracy: {ordering_score}\n")

    print("\nFinish training at:", datetime.now())


# do not make it main because we need to run hypertunity
seed_torch()
args = parse_args()
print(args)
main(args)