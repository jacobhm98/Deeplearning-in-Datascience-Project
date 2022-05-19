import imp
import torch
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader, Dataset
from data_utils import gen_cat_dog_label
# from torchvision.transforms import ToTensor
import torch.nn as nn
from torch.optim import Adam, AdamW
from tqdm.auto import tqdm
import copy
import pandas as pd

from torch.utils.data import DataLoader , TensorDataset   
def append_pseudo_labels(pseudolabels, unlabelled_imgs, transform):
    '''
    labels appended to the unlabeled dataset returns dataloader with transforms as needed
    '''
    pseudo_dataset = TensorDataset(unlabelled_imgs , pseudolabels)
    pseudo_dataloader = DataLoader(pseudo_dataset , batch_size = 16, shuffle=True, transform = transform)

    return pseudo_dataset, pseudo_dataloader

def combine_datasets(pseudo_dataset, train_dataset, transform):
    '''
    combines the two given dataset and returns the combined dataset and combined dataloader.
    '''
    combined_dataset = torch.utils.data.ConcatDataset([train_dataset, pseudo_dataset])
    combined_dataloader = DataLoader(dataset=combined_dataset, transform = transform)

    return combined_dataset, combined_dataloader
class UnsupervisedDataset(Dataset):
    def init(self, ds, labels):
        self.ds = ds
        self.labels = labels
    def getitem(self, i):
        return self.ds[i][0].cuda()
    def len(self):
        return len(self.ds)
'''
Pseudolabelling:
- get unlabelled data
.- make predictions using the model we have, and get the labels
.- call append_pseudolabels
.- call combine_dataset
.- make predictions again
.- test the accuracy, loop for n epoches
'''
def train_model_pseudolabelling(model, train_data, val_data, loss_fxn, optimizer, no_epochs, device, batch_size, cat_dog_dict, 
                cat_dog=True, train_metrics_filename="train_metrics.csv",
                val_metrics_filename="val_metrics.csv", num_workers=2,
                prefetch_factor=2):
    '''
    Parameters:
    cat_dog_dict : {'cat':[cat_id_list], 'dog':[dog_id_list]}
    Return:
    Trained model, loss_arr, acc_arr
    '''

    # If there is a cat dog dict then we want to do classification on 2 species
    cat_dog = cat_dog_dict is not None

    model.to(device)
    train_dataset_size = len(train_data)
    val_dataset_size = len(val_data)

    train_dataloader = DataLoader(train_data, batch_size=batch_size,
                                  shuffle=True, num_workers=8,
                                  prefetch_factor=2)
    # here is val_data is used as unlabelled data, hence the test_dataloader is unlabelled data
    test_dataloader = DataLoader(val_data, batch_size=batch_size,
                                 shuffle=True,
                                 num_workers=8, prefetch_factor=2)
    best_acc = 0.0
    best_model_wts = copy.deepcopy(model.state_dict())
    train_loss_arr = []
    val_loss_arr = []
    train_acc_arr = []
    val_acc_arr = []
    pseudo_data = None

    progress_bar = tqdm(range(no_epochs))
    for i in progress_bar:
        for phase in ['train', 'val']:
            if phase == 'train':
                running_loss = 0.0
                running_corrects = 0
                model.train()  # Set model to training model
                # add pseudolabelling content here
                if pseudo_data != None :
                    transform = None
                    train_data, train_dataloader = combine_datasets(pseudo_data, train_data, transform)
                    train_dataset_size = len(train_data)
                else:
                    print("In first epoch, unlabelled data not used yet!")
                for inputs, labels in tqdm(train_dataloader):
                    inputs = inputs.to(device)
                    if cat_dog:
                        labels = gen_cat_dog_label(cat_dog_dict, labels)

                    labels = labels.to(device)
                    # with torch.set_grad_enabled(True):
                    outputs = model(inputs)
                    preds = torch.argmax(outputs, 1, keepdim=True)
                    loss = loss_fxn(outputs, labels)

                    # Keep track of loss metric
                    train_loss_arr.append(loss.item())

                    # Do backward pass
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

                    running_loss += loss.item()
                    progress_bar.set_postfix_str(loss.item())
                    running_corrects += torch.sum(preds.T == labels.data)
                # Accuracy loss
                epoch_loss = running_loss / train_dataset_size
                # epoch_acc = running_corrects.double() / train_dataset_size
                # train_acc_arr.append(epoch_acc)

                print('{} Loss: {:.4f}'.format(
                    phase, epoch_loss))
            else:
                model.eval()  # Set model to evaluate mode

                # Iterate over data.
                # for pseudolabelling
                input = []
                outputs = []
                for inputs, labels in test_dataloader:
                    inputs, labels = inputs.cuda(), labels.cuda()
                    output = model(inputs)
                    input.append(inputs)
                    preds = torch.argmax(output, dim=1, keepdim=True)
                    outputs.append(preds.T)
                pseudo_data = UnsupervisedDataset(input, outputs)
                # pseudo_data = append_pseudo_labels(outputs, input, transform)
                print("Pseudo data generated!")
    # load best model weights
    model.load_state_dict(best_model_wts)
    df_train = pd.DataFrame({
        'train_loss': train_loss_arr,
    })

    df_val = pd.DataFrame({
        'val_acc': val_acc_arr,
        'val loss': val_loss_arr
    })
    df_train.to_csv(train_metrics_filename)
    df_val.to_csv(val_metrics_filename)
    return model, train_acc_arr,train_loss_arr, val_acc_arr, val_loss_arr




def is_layer_frozen(layer: nn.Module):
    return all([x.requires_grad == True for x in layer.parameters()])


def download_model(model_name, n_layers_to_train, pretrained, lr_per_layer,
                   n_classes, fine_tune_batch_norm=True):
    '''
    Parameters:
    model_name : string -  name of the model
    pretrained : Boolean value- should we use a pretrained model or not
    n_layers_to_train : number of trainable layers
    n_classes :
    fine_tune_batch_norm: True if the batch norm layer parameters are to be
    tuned
    Returns:
    Resnet-18 model
    '''
    model = torch.hub.load('pytorch/vision:v0.10.0', model_name, pretrained=pretrained)
    print("Resnet downloaded")
    parameter_groups = []

    assert 1 <= n_layers_to_train <= 6
    # List that maps the index of a layer to an iterable over its parameters
    layer_i_to_parameters = []
    layer_i_to_parameters.append([model.conv1.weight,
                                  model.bn1.weight,
                                  model.bn1.bias
                                  ])

    # Change final layer
    in_features = model.fc.in_features
    model.fc = nn.Linear(in_features=in_features, out_features=n_classes)

    # Freeze the first layers
    resnet_layer_names = [k for k in model._modules if "layer" in k]
    # layer1, layer2, ...., layern
    resnet_layer_names = sorted(resnet_layer_names,
                                key=lambda x:  int(x[5:]))
    for layer_name in resnet_layer_names:
        layer = model.__getattr__(layer_name)
        layer_i_to_parameters.append(layer.parameters())

    layer_i_to_parameters.append(model.fc.parameters())

    assert 1 <= n_layers_to_train <= len(layer_i_to_parameters), \
        f"Model {model_name} has {len(layer_i_to_parameters)} while " \
        f"n_layers_to_train were {n_layers_to_train}"

    # Freeze layers
    n_layers_to_freeze = len(layer_i_to_parameters) - n_layers_to_train
    for i in range(n_layers_to_freeze):
        for param in layer_i_to_parameters[i]:
            param.requires_grad = False

    # Assign trainable layers to parameter groups
    for i in range(n_layers_to_train):
        parameters = layer_i_to_parameters[-(i+1)]
        parameter_groups.append(
            {'params': parameters, 'lr': lr_per_layer[i]}
        )

    # Dealing with batch norm parameters
    if not fine_tune_batch_norm:
        for name, param in model.named_parameters():
            if "bn" in name:
                param.requires_grad = False

    optimizer = AdamW(parameter_groups)
    return model, optimizer


def print_model_parameter_summary(model):
    for name, param in model.named_parameters():
        print(name, param.size(), param.requires_grad)


def train_model(model, train_data, val_data, loss_fxn, optimizer, no_epochs, device, batch_size, cat_dog_dict,
                cat_dog=True, train_metrics_filename="train_metrics.csv",
                val_metrics_filename="val_metrics.csv", num_workers=2,
                prefetch_factor=2):
    '''
    Parameters:
    cat_dog_dict : {'cat':[cat_id_list], 'dog':[dog_id_list]}
    Return:
    Trained model, loss_arr, acc_arr
    '''

    # If there is a cat dog dict then we want to do classification on 2 species
    cat_dog = cat_dog_dict is not None

    model.to(device)
    train_dataset_size = len(train_data)
    val_dataset_size = len(val_data)

    train_dataloader = DataLoader(train_data, batch_size=batch_size,
                                  shuffle=True, num_workers=num_workers,
                                  prefetch_factor=prefetch_factor)

    best_acc = 0.0
    best_model_wts = copy.deepcopy(model.state_dict())
    train_loss_arr = []
    val_loss_arr = []
    train_acc_arr = []
    val_acc_arr = []

    progress_bar = tqdm(range(no_epochs))
    for epoch_i in progress_bar:
        print("Epoch ", epoch_i)
        for phase in ['train', 'val']:
            if phase == 'train':
                model.train()  # Set model to training mode
                running_loss = 0.0
                running_corrects = 0
                for inputs, labels in tqdm(train_dataloader):
                    inputs = inputs.to(device)
                    if cat_dog:
                        labels = gen_cat_dog_label(cat_dog_dict, labels)

                    labels = labels.to(device)
                    # with torch.set_grad_enabled(True):
                    outputs = model(inputs)
                    preds = torch.argmax(outputs, 1, keepdim=True)
                    loss = loss_fxn(outputs, labels)

                    # Keep track of loss metric
                    train_loss_arr.append(loss.item())

                    # Do backward pass
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

                    running_loss += loss.item()
                    progress_bar.set_postfix_str(loss.item())
                    running_corrects += torch.sum(preds.T == labels.data)

                # Accuracy loss
                epoch_loss = running_loss / train_dataset_size
                epoch_acc = running_corrects.double() / train_dataset_size
                # train_acc_arr.append(epoch_acc)

                print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                    phase, epoch_loss, epoch_acc))

            elif phase == 'val':
                epoch_loss, epoch_acc = test_loss_and_accuracy(
                    val_data,
                    model,
                    loss_fxn,
                    device,
                    cat_dog_dict,
                    num_workers=num_workers,
                    prefetch_factor=prefetch_factor
                )

                # Test accuracy and loss on validation set
                val_acc_arr.append(epoch_acc.item())
                val_loss_arr.append(epoch_loss)
                print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                    phase, epoch_loss, epoch_acc))
                if phase == 'val' and epoch_acc > best_acc:
                    best_acc = epoch_acc
                    best_model_wts = copy.deepcopy(model.state_dict())

    print('Best val Acc: {:4f}'.format(best_acc))

    # load best model weights
    model.load_state_dict(best_model_wts)
    df_train = pd.DataFrame({
        'train_loss': train_loss_arr,
    })

    df_val = pd.DataFrame({
        'val_acc': val_acc_arr,
        'val loss': val_loss_arr
    })
    df_train.to_csv(train_metrics_filename)
    df_val.to_csv(val_metrics_filename)
    return model, train_acc_arr, train_loss_arr, val_acc_arr, val_loss_arr


def test_loss_and_accuracy(dataset, model, loss_fxn, device,
                           cat_dog_dict=None,
                           num_workers=2,
                           prefetch_factor=2
                           ):
    cat_dog = cat_dog_dict is not None
    dataloader = DataLoader(dataset, batch_size=64, shuffle=True,
                            num_workers=num_workers, prefetch_factor=prefetch_factor)
    dataset_size = len(dataset)
    model.eval()  # Set model to evaluate mode
    running_loss = 0.0
    running_corrects = 0
    with torch.no_grad():
        # Iterate over data.
        for inputs, labels in dataloader:
            inputs = inputs.to(device)
            if cat_dog:
                labels = gen_cat_dog_label(cat_dog_dict, labels)
            labels = labels.to(device)
            outputs = model(inputs)
            preds = torch.argmax(outputs, dim=1, keepdim=True)
            loss = loss_fxn(outputs, labels)
            running_loss += loss.item()
            running_corrects += torch.sum(preds.T == labels.data)

    epoch_loss = running_loss / dataset_size
    epoch_acc = running_corrects.double() / dataset_size
    return epoch_loss, epoch_acc
