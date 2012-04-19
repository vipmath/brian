'''
Notes:

Based on Issam's forDan/178_end.cu.

The idea is that for each spiking neuron i, it is connected to tgt[off[i]+k] for
0<=k<numsynapses[i], and we vectorise over k. This looks something like this if
neurons 0, 3 and 4 spiked:

i    tgt[off[i]] ...        numsynapses[i]
0    2  4  5  8  15         5
3    3  9                   2
4    0  1  2                3

We launch max(numsynapses) threads, so we waste time if the structure is very
ragged, but is relatively efficient if it is uniform (i.e. numsynapses[i]
const).

The kernel looks like this:
    __global__ ...
        int thread = ...
        targets = tgt_arr+off[i];
        weights = w_arr+off[i];
        for(i=0;i<numspikes;i++)
            if(i<numsynapses[i])
                j = targets[thread]; // coalesced read
                w = weights[thread]; // coalesced read
                atomic V[j]+=w;      // uncoalesced read/write

Theoretical analysis of performance:
- read of targets and weights is nicely coalesced
- if numsynapses~10000 then we are using plenty of threads, efficient use
- writing to target state variable is uncoalesced, inefficient
    + means that we typically have long waits for each synapse, must be
      inefficient?
- writing to target state variable is atomic, inefficient
    + this cost depends on the number of conflicts, but in many cases may not
      be too bad      
'''

from brian import *
from brian.experimental.codegen2 import *
import numpy
import time
import numpy.random as nrandom
import random as prandom
try:
    import pycuda
except ImportError:
    pycuda = None    

__all__ = ['GPUConnection']

class GPUConnection(Connection):
    '''
    Only works with sparse at the moment, no modulation, no delays
    '''
    def __init__(self, *args, **kwds):
        self.use_atomic = kwds.pop('use_atomic', False)
        super(GPUConnection, self).__init__(*args, **kwds)
    def gpu_prepare(self):
        self._gpu_prepared = True
        # find target variable on GPU
        self.gpuman = gpuman = self.target._state_updater.language.gpu_man
        self.memman = memman = gpuman.mem_man
        self.gpu_target_var = self.memman.device['_arr_'+self.state]
        # upload relevant data
        W = self.W
        self.gpu_alldata = pycuda.gpuarray.to_gpu(W.alldata)
        self.gpu_rowind = pycuda.gpuarray.to_gpu(W.rowind)
        self.gpu_allj = pycuda.gpuarray.to_gpu(W.allj)
        self.numsynapses = array([len(row) for row in W.rowdata], dtype=int)
        self.gpu_numsynapses = pycuda.gpuarray.to_gpu(self.numsynapses)
        self.gpu_numthreads = amax(self.numsynapses)
        self.gpu_spikes = pycuda.gpuarray.empty(len(self.source), dtype=int)
        # define propagation kernel
        self.gpu_code = '''
        // Issam's version
        /*__device__ inline void atomicAdd(double *address, double val)
        {
             unsigned long long int i_val = ((unsigned long long int*) &val)[0];
             unsigned long long int tmp0 = 0;
             unsigned long long int tmp1;
             double interm;
             while( (tmp1 = atomicCAS((unsigned long long int *)address, tmp0, i_val)) != tmp0)
             {     
                     tmp0 = tmp1;
                     interm = val + ((double*) &tmp1 )[0] ;
                     i_val = ((unsigned long long int*) &interm)[0];             
             }
        }*/
        // CUDA manual version
        __device__ double atomicAdd(double* address, double val)
        {
            unsigned long long int* address_as_ull = (unsigned long long int*)address;
            unsigned long long int old = *address_as_ull, assumed;
            do {
                assumed = old;
                old = atomicCAS(address_as_ull, assumed,
                        __double_as_longlong(val + __longlong_as_double(assumed)));
            } while (assumed != old);
            return __longlong_as_double(old);
        }        
        // propagate function, modified from Issam's code
        __global__ void propagate(double *v, double *alldata, int64_t *rowind,
                                  int64_t *allj, int64_t *numsynapses,
                                  int64_t target_offset,
                                  int64_t *spikes, int64_t numspikes)
        {
            //return;
            v = v+target_offset;
            const int synaptic_offset = blockIdx.x * blockDim.x + threadIdx.x;
            for(int spikes_index=0; spikes_index<numspikes; spikes_index++)
            {
                const int spike = spikes[spikes_index];
                if(synaptic_offset<numsynapses[spike])
                {
                    const int target = allj[rowind[spike]+synaptic_offset]; // coalesced
                    const double weight = alldata[rowind[spike]+synaptic_offset]; // coalesced
                    %PROPAGATE%
                    //v[target] += weight; // uncoalesced, incorrect, but no atomics
                    //atomicAdd(v+target, weight); // uncoalesced, correct
                }
            }
        }
        '''
        if self.use_atomic:
            self.gpu_code = self.gpu_code.replace('%PROPAGATE%',
                    'atomicAdd(v+target, weight);')
        else:
            self.gpu_code = self.gpu_code.replace('%PROPAGATE%',
                    'v[target] += weight;')
        self.gpu_module = pycuda.compiler.SourceModule(self.gpu_code)
        self.gpu_func = self.gpu_module.get_function('propagate')
        log_info('brian.GPUConnection',  'local size: '+str(self.gpu_func.local_size_bytes))
        log_info('brian.GPUConnection',  'shared size: '+str(self.gpu_func.shared_size_bytes))
        log_info('brian.GPUConnection',  'num regs: '+str(self.gpu_func.num_regs))
        self.block, self.grid = compute_block_grid(512, self.gpu_numthreads)
        log_info('brian.GPUConnection', 'block='+str(self.block)+', grid='+str(self.grid))
        self.gpu_func.prepare(
                (numpy.intp, numpy.intp, numpy.intp, numpy.intp, numpy.intp,
                 numpy.int64, numpy.intp, numpy.int64),
                self.block)
        self.gpu_args = (numpy.intp(self.gpu_target_var.gpudata),
                         numpy.intp(self.gpu_alldata.gpudata),
                         numpy.intp(self.gpu_rowind.gpudata),
                         numpy.intp(self.gpu_allj.gpudata),
                         numpy.intp(self.gpu_numsynapses.gpudata),
                         numpy.int64(self.target._origin),
                         numpy.intp(self.gpu_spikes.gpudata),
                         )
        
    def propagate(self, spikes):
        if not hasattr(self, '_gpu_prepared'):
            self.gpu_prepare()
        if len(spikes)==0:
            return
        if not isinstance(spikes, numpy.ndarray):
            spikes = array(spikes, dtype=int)
        pycuda.driver.memcpy_htod(int(self.gpu_spikes.gpudata), spikes)
        if self.gpuman.force_sync:
            self.memman.copy_to_device(True)
        args = self.gpu_args+(numpy.int64(len(spikes)),)
        self.gpu_func.prepared_call(self.grid, *args)
        if self.gpuman.force_sync:
            self.memman.copy_to_host(True)
