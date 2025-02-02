from typing import List
import numpy as np
import torch
from torch import Tensor
from botorch.test_functions import SyntheticTestFunction


PARAMS = {
    'demand_mean' : 5, 
    'demand_std' : 2,
    'lead_time' : 10, # tau in the slides
    'stockout_penalty' : 0.1, # p in the slides
    'holding_cost' : 0.01, # h in the slides
    'K' : 1, 
    'c' : 0.1 # order cost parameters
}

# code credit to Peter
def step(state, Q, R, params):
    r"""
    Run one day forward and return the updated state.

    Args:
        state: a dictionary tracking 'inventory', 'days_left', 'cost'
        Q: order quantity
        R: reorder level
        params: a dictionary of parameters
    """

    # Check whether an order has arrived
    if state['days_left'] == 0:
        # If it has, unload the truck and add the contents to our inventory
        state['inventory'] += Q
        # Mark the state to say that a new order hasn't been placed
        state['days_left'] = -1
    # Otherwise, if an order is in transit, it gets one day closer
    elif state['days_left']>0:
        state['days_left'] -= 1
    
    # Simulate the demand on this day, and fill it from our inventory.
    # This may make our inventory negative.
    # Also calculate stockout costs
    demand = np.random.normal(params['demand_mean'], params['demand_std'])
    demand = np.max([0,demand])
    
    # Calculate stockout costs
    onhand = np.max([0,state['inventory']])
    backordered = np.max([0,demand - onhand])
    state['cost']+= params['stockout_penalty'] * backordered
    
    # Fill demand from inventory, backordering if needed
    state['inventory'] -= demand
    
    # Pay holding cost
    state['cost'] += params['holding_cost']*np.max([state['inventory'],0])
    
    # If our inventory is below the reorder level, and an order is 
    # not on its way, then place an order
    if state['inventory'] < R and state['days_left'] == -1:
        state['days_left'] = params['lead_time']
        state['cost'] += params['K'] + params['c']*Q
        
    return state


class Inventory(SyntheticTestFunction):
    r"""
    Simulate an inventory control problem using the QR policy.
    The QR policy is a 2-parameter strategy that reorders quantity Q when the 
    inventory level drops below reorder level R.
    Each policy leads to a trajectory of inventory levels over time.
    """
    dim = 2
    _bounds = torch.tensor([[0., 1.], [0., 1.]]) 

    def __init__(
        self,
        duration: int,
        init_inventory: int = 50, # keep it fixed for now
        x_baseline: int = 50,
        x_scaling: int = 50, # scale x to be between 50 and 100
        params: dict = PARAMS
    ):
        r"""
        Args:
            duration: number of days to simulate
            init_inventory: initial inventory level
            x_baseline: lower bound for actual values of Q,R params
            x_scaling: scale Q,R values to be in [x_baseline, x_baseline + x_scaling]
            params: dictionary of parameters for running the simulation
        """
        super().__init__()
        self.outcome_dim = duration
        self.init_inventory = init_inventory
        self.x_baseline = x_baseline
        self.x_scaling = x_scaling
        self.params = params

    def evaluate_true(self, X: Tensor):
        r"""
        Args:
            X: `num_samples x 2` tensor, where first column is Q and second is R
        """
        Y = []
        for i, x in enumerate(X):
            Y.append(self.evaluate_true_single(
                i, self.x_baseline + self.x_scaling * x))
        
        return torch.vstack(Y)

    def evaluate_true_single(self, i: int, x: Tensor):
        r"""
        Simulate an inventory trajectory for one policy.
        Args:
            i: index of x in all data, used to set random seed
            x: 2-dim tensor, where first entry is Q and second is R
        """

        # np.random.seed(i)
        # init_inventory = np.random.rand() * 50 + 50 # doesn't work very well
        init_inventory = self.init_inventory

        x_np = x.detach().numpy()
        state = {
            'inventory' : init_inventory, 
            'days_left' : -1, 
            'cost' : 0
        }
        inventory = []
        days_left = []
        cost = []

        for t in range(self.outcome_dim):
            inventory.append(state['inventory'])
            days_left.append(state['days_left'])
            cost.append(state['cost'])
            state = step(state, Q=x_np[0], R=x_np[1], params=self.params)

        return torch.tensor(inventory) 
        

class InventoryUtil(torch.nn.Module):
    r"""
    Negative cost incurred by an inventory time series.
    Naive version: cost incurred by ordering, holding, and backordering.
    TODO: Fancy version: incorporate latent preferences, 
        e.g., someone who dislikes when the inventory gets above a threshold 
        because it doesn't fit in the warehouse, or when Q is above a threshold 
        because you need to send two trucks
    """
    def __init__(
        self,
        stockout_penalty_per_unit: float = 0.1, # p in the slides
        holding_cost_per_unit: float = 0.01, # h in the slides
        order_cost_one_time: float = 1,
        order_cost_per_unit: float = 0.1,
    ):
        super().__init__()
        self.stockout_penalty_per_unit = stockout_penalty_per_unit
        self.holding_cost_per_unit = holding_cost_per_unit
        self.order_cost_one_time = order_cost_one_time
        self.order_cost_per_unit = order_cost_per_unit


    def forward(self, Y: Tensor):
        r"""
        Compute the utility associated with a set of inventory time series.

        Args:
            Y: `num_samples x outcome_dim` tensor
        """

        neg_costs = []

        for y in Y:
            neg_costs.append(self.compute_neg_cost(y))
        
        return torch.tensor(neg_costs).unsqueeze(1)

    def compute_neg_cost(self, y: Tensor):
        r"""
        Compute the utility for one inventory time series.
        
        Args:
            y: k-dimensional tensor
        """

        cost = 0.
        y = y.detach().numpy()

        for t in range(len(y)):
            cost += self.holding_cost_per_unit * np.maximum(float(y[t]), 0.)
            cost += self.stockout_penalty_per_unit * np.maximum(-y[t], 0.)
            if t > 0 and y[t] > y[t-1]:
                cost += self.order_cost_one_time + self.order_cost_per_unit * (y[t]-y[t-1])
        
        return -cost
    

# Wrappers for multiple products

class MultipleInventories(SyntheticTestFunction):
    def __init__(
        self,
        problem_list: List[Inventory]
    ):
        self.dim = 2 * len(problem_list)
        self._bounds = torch.tensor([[0.,1.]]*self.dim)
        super().__init__()
        self.outcome_dim = sum([problem.outcome_dim for problem in problem_list])
        self.problem_list = problem_list
    
    def evaluate_true(self, X:Tensor):
        Y = []
        for problem in self.problem_list:
            Y.append(problem.evaluate_true(X))
        
        return torch.hstack(Y)
    

class MultipleInventoriesUtil(torch.nn.Module):
    def __init__(
        self,
        individual_duration: int,
        util_func_list: List[InventoryUtil],
    ):
        super().__init__()
        self.util_func_list = util_func_list
        self.individual_duration = individual_duration
    
    def forward(self, Y: Tensor):
        neg_costs = []
        for i, util_func in enumerate(self.util_func_list):
            neg_costs.append(util_func(Y[:, i*self.individual_duration:(i+1)*self.individual_duration]))
        
        return torch.sum(torch.hstack(neg_costs), dim=1, keepdim=True)