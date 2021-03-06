import os
import numpy as np
import torch
import torch.nn as nn
import torch.autograd as autograd
import time
from numpy.linalg import norm
import copy
import math
from numpy.linalg import cholesky
import argparse


class Net_timestep(nn.Module):
    
    def __init__(self, dim, nOut, n_layers, vNetWidth, activation = "relu"):
        super(Net_timestep, self).__init__()
        self.dim = dim
        self.nOut = nOut
        
        if activation == "relu":
            self.activation = nn.ReLU()
        elif activation == "tanh":
            self.activation = nn.Tanh()
        else:
            raise ValueError("unknown activation function {}".format(activation))
       

        
        self.i_h = self.hiddenLayerT0(dim, vNetWidth)
        self.h_h = nn.ModuleList([self.hiddenLayerT1(vNetWidth, vNetWidth) for l in range(n_layers-1)])
        self.h_o = self.outputLayer(vNetWidth, nOut)
        
    def hiddenLayerT0(self,  nIn, nOut):
        layer = nn.Sequential(nn.BatchNorm1d(nIn, momentum=0.1), nn.Linear(nIn,nOut,bias=True),
                              nn.BatchNorm1d(nOut, momentum=0.1),   
                              self.activation)   
        return layer
    
    def hiddenLayerT1(self, nIn, nOut):
        layer = nn.Sequential(nn.Linear(nIn,nOut,bias=True),
                              nn.BatchNorm1d(nOut, momentum=0.1),  
                              self.activation)   
        return layer
    
    
    def outputLayer(self, nIn, nOut):
        layer = nn.Sequential(nn.Linear(nIn, nOut,bias=True),
                              nn.BatchNorm1d(nOut, momentum=0.1))
        return layer
    
    def forward(self, S):
        h = self.i_h(S)
        for l in range(len(self.h_h)):
            h = self.h_h[l](h)
        output = self.h_o(h)
        return output  



class ControlVariate_stoch_int(nn.Module):
    """
    Main reference: https://arxiv.org/abs/1806.00421 
    """
    
    def __init__(self, dim, r, sigma, covariance_mat, timegrid, n_layers, vNetWidth = 100, gradNetWidth=100):
        super(ControlVariate_stoch_int, self).__init__()
        self.dim = dim
        self.timegrid = torch.Tensor(timegrid).to(device)
        self.r = r # r is a number
        self.sigma = torch.Tensor(sigma).to(device) # this should be a vector of length dim
        self.covariance_mat = covariance_mat # covariance matrix
        self.C = cholesky(covariance_mat) # Cholesky decomposition of covariance matrix, with size (dim,dim)
        
        self.volatility_mat = torch.Tensor(self.C).to(device)
        for i in range(self.dim):
            self.volatility_mat[i] = self.volatility_mat[i]*self.sigma[i]

        self.net_timegrid = nn.ModuleList([Net_timestep(dim=dim, nOut=dim, n_layers=n_layers, vNetWidth=vNetWidth) for t in timegrid[:-1]])  



    def forward(self, S0):                                                                           
        S_old = S0
        control_variate = 0 
        path = [S_old]
        
        for i in range(1,len(self.timegrid)):
            # Wiener process at time timegrid[i]
            h = self.timegrid[i]-self.timegrid[i-1]
            dW = math.sqrt(h)*torch.randn(S_old.data.size(), device=device)#.to(device)
                        
            # volatility(t,S) * dW
            volatility_of_S_dW = S_old * torch.matmul(self.volatility_mat,dW.transpose(1,0)).transpose(1,0) # this is a matrix of size (batch_size x dim)
            
            # gradient of value function
            Z = torch.exp(-self.r * self.timegrid[i-1]) * self.net_timegrid[i-1](S_old)
            
            # stochastic integral
            stoch_int = torch.bmm(Z.unsqueeze(1), volatility_of_S_dW.unsqueeze(2)).squeeze(1)    
            
            # control variate
            control_variate += stoch_int
                
            # we update the SDE path. Use one or the other. 
            S_new = S_old  + self.r*S_old*h + volatility_of_S_dW 
                
            # we are done, prepare for next round
            S_old = S_new
            path.append(S_old)
                
        return S_old, control_variate, path


def g(S, S0):
    """
    basket options
    """
    zeros = torch.zeros(S.size()[0],1, device=device)
    K = S0.sum(1).view(-1,1)
    sum_final = S.sum(1).view(-1,1)
    m = torch.cat([zeros, sum_final - K],1)
    output = torch.max(m,1)
    return output[0]


def train_optimise_var():
    model.train()
    for it in range(n_iter):
        model.train()
        optimizer.zero_grad()
        
        # learning rate decay
        lr = base_lr * 0.1**(it//1000)
        for param_group in optimizer.state_dict()['param_groups']:
            param_group['lr'] = lr
        
        z = torch.randn([batch_size, dim], device=device)#.to(device)
        input = torch.exp((mu-0.5*sigma**2)*tau + math.sqrt(tau)*z)*0.7
        
        init_time = time.time()
        S_T, control_variate, _ = model(input)
        time_forward = time.time() - init_time
        
        K = torch.ones_like(S_T)
        terminal = torch.exp(torch.tensor([-T*r], device=device))*g(S_T,K).view(-1,1)
        MC_CV = terminal - control_variate
        var_MC_CV_estimator = MC_CV.var()
        loss = var_MC_CV_estimator
        
        init_time = time.time()
        loss.backward()
        time_backward = time.time() - init_time
        
        optimizer.step()
        
        with open(file_log_path, 'a') as f:
            f.write("Iteration=[{it}/{n_iter}]\t loss={loss:.8f}\t time forward pass={t_f:.3f}\t time backward pass={t_b:.3f}\n".format(it=it, n_iter=n_iter, loss=loss.item(), t_f=time_forward, t_b=time_backward))
    
        if (it+1) % 100 == 0:
            var_MC_CV_estimator, var_MC_estimator, MC_CV_estimator, MC_estimator, corr_terminal_control_variate = get_prediction_CV(1000)
            with open(file_log_results, 'a') as f:
                f.write('{},{},{},{},{}\n'.format(var_MC_CV_estimator, var_MC_estimator, MC_CV_estimator, MC_estimator, corr_terminal_control_variate))
        
        if (it+1) % 1000 == 0:
            state = {'epoch':it+1, 'state_dict':model.state_dict(), 'optimizer':optimizer.state_dict()}
            filename = 'model_variance_'+str(n_layers)+'_'+str(vNetWidth)+'_'+str(timestep)+'_'+str(dim)+'_it'+str(it)+'.pth.tar'
            torch.save(state, filename)
            
    print("Done.")




def get_prediction_CV(batch_size_MC=100000):
    model.eval()
    
    if batch_size_MC > 1000:
        terminal_list = []
        control_variate_list = []
        for i in range(batch_size_MC//1000):
            print(i)
            input = torch.ones(1000, dim, device=device)*0.7
            with torch.no_grad():
                S_T, control_variate, _ = model(input)
            K = torch.ones_like(S_T)
            terminal = torch.exp(torch.tensor([-T*r], device=device))*g(S_T,K).view(-1,1)
            terminal_list.append(terminal)
            control_variate_list.append(control_variate)
        terminal = torch.cat(terminal_list, 0)
        control_variate = torch.cat(control_variate_list, 0)
    else:
        input = torch.ones(batch_size_MC, dim, device=device)*0.7
        with torch.no_grad():
            S_T, control_variate, _ = model(input)
        terminal = torch.exp(torch.tensor([-T*r], device=device))*g(S_T,input).view(-1,1)
    MC_estimator = torch.mean(terminal)
    var_terminal = torch.mean((terminal - torch.mean(terminal))**2)
    var_MC_estimator = 1/batch_size_MC*var_terminal
    
    cov_terminal_control_variate = torch.mean((terminal-torch.mean(terminal))*(control_variate-torch.mean(control_variate)))
    var_control_variate = control_variate.var()#torch.mean((control_variate-torch.mean(control_variate))**2)
    corr_terminal_control_variate = cov_terminal_control_variate/(torch.sqrt(var_control_variate)*torch.sqrt(var_terminal))
    
    # Optimal coefficent b that minimises variance of optimally controlled estimator   
    b = cov_terminal_control_variate / var_control_variate
    
    # Monte Carlo controlled iterations
    MC_CV = terminal - b*(control_variate)
    
    # Monte Carlo controlled estimator
    MC_CV_estimator = torch.mean(MC_CV)
    
    var_MC_CV_estimator = 1/batch_size_MC * MC_CV.var()#var_MC_CV_estimator 
    
    return var_MC_CV_estimator.item(), var_MC_estimator.item(), MC_CV_estimator.item(), MC_estimator.item(), corr_terminal_control_variate.item()




if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--vNetWidth', action="store", type=int, default=22, help="network width")
    parser.add_argument('--n-layers', action="store", type=int, default=2, help="number of layers")
    parser.add_argument('--timestep', action="store", type=float, default=0.01, help="timestep")
    parser.add_argument('--dim', action="store", type=int, default=2, help="dimension of the PDE")

    args = parser.parse_args()
    vNetWidth = args.vNetWidth
    n_layers = args.n_layers
    timestep = args.timestep
    dim = args.dim

    if torch.cuda.is_available():
        device = "cuda:0"
    else:
        device = "cpu"

    PATH_RESULTS = os.getcwd()
    os.chdir(PATH_RESULTS)

    log_results = 100
    file_log_path = os.path.join(PATH_RESULTS, 'log_var_opt_'+str(dim)+'.txt')
    file_log_results = os.path.join(PATH_RESULTS, 'results_var_opt_'+str(dim)+'.txt')
    with open(file_log_results, 'a') as f:
        f.write('var_MC_CV_estimator, var_MC_estimator, MC_CV_estimator, MC_estimator,corr_terminal_control_variate\n')
    
    ##################
    # Problem setup ##
    ##################
    init_t, T = 0,1
    timegrid = np.arange(init_t, T+timestep/2, timestep)
    r = 0.5
    sigma = 1
    mu = 0.08
    tau = 0.1
    covariance_mat = np.identity(dim)    
    
    #########################
    # Network instantiation #
    #########################
    model = ControlVariate_stoch_int(dim=dim, r=r, sigma=np.array([sigma]*dim), covariance_mat=covariance_mat, timegrid=timegrid, n_layers=n_layers, vNetWidth=vNetWidth, gradNetWidth=vNetWidth)  
    model.to(device)
    
    #######################
    # training parameters #
    #######################
    batch_size = 5000
    base_lr = 0.001
    optimizer = torch.optim.Adam(model.parameters(),lr=base_lr, betas=(0.9, 0.999))
    n_iter = 20000
    
    train_optimise_var()
