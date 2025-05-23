import argparse
import pandas as pd
import numpy as np
from scipy import stats
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset

from model import CathPred
import random
from config import Config

def train(model, train_dataloader, optimizer, loss_fn, device):
    # Set model to training mode
    model.train()
    # Initialize running loss function value
    train_loss = 0.0

    # Loop over the batches of data
    for x, y in train_dataloader:
        for k,v in x.items():
            x[k] = v.to(device, non_blocking=True)
        y = y.to(device, non_blocking=True)

        # Zero the parameter gradients
        optimizer.zero_grad()

        # Forward pass: get outputs by passing inputs to the model
        with torch.autocast("cuda"):
            outputs = model(x)
            # Compute loss: compare outputs with labels
            loss = loss_fn(outputs, y)

        # Backward pass: compute gradients of the loss with respect to model parameters
        loss.backward()
        # Update parameters: perform a single optimization step (parameter update)
        optimizer.step()

        # Record statistics
        train_loss += loss.item() # * x.size(0) # total loss of the batch (not averaged) #TODO: batch = 1 irrelevant 

    train_loss = train_loss / len(train_dataloader.dataset) # loss averaged across all training examples for the current epoch

    return train_loss


def evaluate(model, val_dataloader, loss_fn, device):
    # Set model to evaluation mode
    model.eval()
    # Initialize validation loss
    val_loss = 0.0

    # Disable gradient computation and turn it back on after the validation loop is finished
    with torch.no_grad():
        for x, y in val_dataloader:
            for k,v in x.items():
                x[k] = v.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)
            
            with torch.autocast("cuda"):
                outputs = model(x)
                loss = loss_fn(outputs, y)

            val_loss += loss.item() # * x.size(0) #TODO: batch = 1 irrelevant

    val_loss = val_loss / len(val_dataloader.dataset)

    return val_loss

def make_prediction(model, test_dataloader, device):
    # Initialize lists or tensors to store outputs and labels
    pred_list = []

    # Set model to evaluation mode
    model.eval()

    # Disable gradient computation
    with torch.no_grad():

      # Loop over the batches of test data
      for x_test, y_test in test_dataloader:
        for k,v in x_test.items():
            x_test[k] = v.to(device, non_blocking=True)
        y_test = y_test.to(device, non_blocking=True)

        # Forward pass: get outputs by passing inputs to the model
        with torch.autocast("cuda"):
            pred = model(x_test)

        # Append outputs and labels to lists or tensors
        pred_list.append(pred)
    
    pred_tensor = torch.cat(pred_list, dim=0)
        
    return pred_tensor

class CathPredDataset(Dataset):
    def __init__(self, domainID, proteinID, embedding, startDomain, endDomain, y):
        self.domainID = torch.tensor(domainID, dtype = torch.int64) #TODO: String instead? 
        self.proteinID = torch.tensor(proteinID, dtype = torch.int64) #TODO: String instead? 
        self.embedding = embedding # torch.tensor(embedding, dtype = torch.float64)
        self.startDomain = torch.tensor(startDomain, dtype = torch.int64)
        self.endDomain = torch.tensor(endDomain, dtype = torch.int64)
        self.y = torch.tensor(y, dtype = torch.int64)
        
    def __len__(self):
        return len(self.y)
    
    def __getitem__(self, index):
        x = {"domainID":self.domainID[index],
             "proteinID":self.proteinID[index],
             "embedding":self.embedding[index],
             "startDomain":self.startDomain[index],
             "endDomain": self.endDomain[index]
             }
        y = self.y[index]
        return x, y

def multiLevelCATHLoss(class_pred, class_true, weights = [1,1,1,1]):
    # TODO: currently only class level 
    loss = torch.nn.CrossEntropyLoss()
    total_loss = 0
    for pred, true, weight in zip(class_pred, class_true, weights):
        total_loss += weight * loss(pred,true)
    return total_loss

def main():
    
    parser = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter
        )

    test = parser.add_argument_group(title = 'Test parameters',
                                     description = 'Parameters for testing model')
    test.add_argument('-e', '--epoch', default = 50, type = int, 
                      help = 'Epoch number for model training')
    test.add_argument('-b', '--batch', default = 1, type = int, 
                      help = 'Batch size for model training')
    test.add_argument('-s', '--split', default = 0.7, type = float,
                      help = 'Proportion for the training dataset')
    test.add_argument('-l', '--learning', default = 0.0005, type = float, 
                      help = 'Learning rate for model training')
    test.add_argument('-d', '--weight_decay', default = 0.01, type = float, 
                      help = 'Weight decay (L2 penalty)')
    test.add_argument('-w', '--warmup', default = 5, type = int, 
                      help = 'Warm-up epochs for model training')
    test.add_argument('-save', '--save', action = 'store_true', 
                      help = 'Save the model and training results')

    run = parser.add_argument_group(title = 'Prediction parameters',
                                    description = 'Parameters for Prediction using model')
    run.add_argument('-o', '--output', help = 'Output pytorch model')
    run.add_argument('-i', '--input_folder', help='Input data folder')

    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    print("--------------------------------------------------\nData loading")
    
    path = os.getcwd()
    print(path)
    datapath = path + '/datasets/' + args.input_folder + '/'
    print(f"Loading dataset: {datapath}")
    
    all_files = os.listdir(datapath)
    xc_file = [f for f in all_files if f.endswith('xc.pkl')][0]
    yc_file = [f for f in all_files if f.endswith('yc.pkl')][0]
    x_c = pd.read_pickle(os.path.join(datapath, xc_file))
    y_c = pd.read_pickle(os.path.join(datapath, yc_file))
    assert x_c.shape[0]==y_c.shape[0]
    data_size = x_c.shape[0]
    num_train = int(args.split * data_size)
    num_val = (data_size - num_train) // 2
    
    # model configs
    model_config = Config()
    with open(os.path.join(datapath, "config.txt"), "w") as f:
        f.write(model_config.__repr__())
    
    # Reformat y to numpy matrix
    ymat = y_c["C"].to_numpy() # TODO: only single class

    # randomly shuffle the data
    indices = np.random.default_rng(seed=42).permutation(data_size)
    training_idx, test_idx, val_idx = np.split(indices, [num_train, num_train + num_val])

    x_train, y_train = x_c.iloc[training_idx], ymat[training_idx]
    x_val, y_val = x_c.iloc[val_idx], ymat[val_idx]
    x_test, y_test = x_c.iloc[test_idx], ymat[test_idx]

    print(len(x_train), "Training sequences")
    print(len(x_val), "Validation sequences")
    print(len(x_test), "Testing sequences")

    # initiate the model
    model = CathPred(model_config)
    model.to(device)

    # Create DataLoaders
    train_dataset = CathPredDataset(
        domainID=x_train["domainID"].values,
        proteinID=x_train["proteinID"].values,
        embedding=x_train["embedding"].values,
        startDomain=x_train["startDomain"].values,
        endDomain=x_train["endDomain"].values,
        y=y_train
    )
    train_dataloader = DataLoader(train_dataset, batch_size=args.batch, shuffle=True, pin_memory=True, num_workers=14)

    val_dataset = CathPredDataset(
        domainID=x_val["domainID"].values,
        proteinID=x_val["proteinID"].values,
        embedding=x_val["embedding"].values,
        startDomain=x_val["startDomain"].values,
        endDomain=x_val["endDomain"].values,
        y=y_val
    )
    val_dataloader = DataLoader(val_dataset, batch_size=args.batch, pin_memory=True, num_workers=14)

    test_dataset = CathPredDataset(
        domainID=x_test["domainID"].values,
        proteinID=x_test["proteinID"].values,
        embedding=x_test["embedding"].values,
        startDomain=x_test["startDomain"].values,
        endDomain=x_test["endDomain"].values,
        y=y_test
    )
    test_dataloader = DataLoader(test_dataset, batch_size=args.batch, pin_memory=True, num_workers=14)


    # Define optimizer with a scheduler
    optimizer = optim.AdamW(model.parameters(), lr=args.learning, weight_decay=args.weight_decay)
    warmup_scheduler = optim.lr_scheduler.LinearLR(optimizer, start_factor = 0.001, total_iters = args.warmup)
    train_scheduler = optim.lr_scheduler.LinearLR(optimizer, start_factor = 1.0, end_factor = 0.01, total_iters = (args.epoch - args.warmup))
    scheduler = optim.lr_scheduler.SequentialLR(optimizer, [warmup_scheduler, train_scheduler], [args.warmup])
    
    # Define loss criterion
    loss_fn = multiLevelCATHLoss

    print("--------------------------------------------------\nTraining")

    # Training loop. 1 epoch = 1 Loop over the dataset:
    best_loss = np.inf
    best_epoch = 0
    for epoch in range(args.epoch):
        train_loss = train(model, train_dataloader, optimizer, loss_fn, device)
        val_loss = evaluate(model, val_dataloader, loss_fn, device)
        scheduler.step()

        print(f"Epoch {epoch+1}/{args.epoch}")
        print(f"Train Loss: {train_loss:.4f}")
        print(f"Val Loss: {val_loss:.4f}")
        print()

        # save best performance
        if val_loss < best_loss:
            best_loss = val_loss
            best_epoch = epoch
            if args.save: 
               torch.save(model.state_dict(), datapath + args.output + ".pt")
        
        # early stopping if loss does not imporve for a patience of 10 epochs
        if (epoch-best_epoch)==10:
            print(f"Early stopping at epoch {epoch+1}")
            break
            
    # load best model
    model.load_state_dict(torch.load(datapath + args.output + ".pt"))
    
    # output all the correlations
    out_tensors = make_prediction(model, test_dataloader, device)
    testdf = None # TODO: define test df with out_tensors
    testdf.to_parquet(datapath + 'model_prediction.parquet')
    
    train_unshuffled = DataLoader(train_dataset, batch_size=args.batch, shuffle=False, pin_memory=True, num_workers=14)
    out_tensors = make_prediction(model, train_unshuffled, device)
    traindf = None # TODO: define train df with out_tensors
    traindf.to_parquet(datapath + 'model_trainingset.parquet')
    
    print("--------------------------------------------------\nFinished!")

if __name__ == '__main__':
    main()
