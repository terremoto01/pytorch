from __future__ import annotations

import contextlib
import dataclasses
import functools
import gc
import itertools
import threading
import warnings
import weakref
from collections import defaultdict

from enum import auto, Enum
from typing import Any, Callable, Dict, Generator, List, Optional, Sequence, Set, Tuple

import torch.fx
from torch import Tensor
from torch._dynamo.mutation_guard import GenerationTracker
from torch._inductor.compile_fx import (
    get_expanded_dims,
    index_expanded_dims,
    remove_unaligned_input_idxs,
    static_input,
)
from torch._prims_common import check
from torch.multiprocessing.reductions import StorageWeakRef
from torch.utils import _pytree as pytree

StorageWeakRefPointer = int


if torch.has_cuda:
    from torch._C import _cuda_CUDAAllocator_AllocatorState as AllocatorState
else:
    AllocatorState = Any

from . import config


@dataclasses.dataclass(frozen=True)
class GraphID:
    "Unique counter of a cuda graph recording"
    id: int


@dataclasses.dataclass(frozen=True)
class FunctionID:
    "Unique counter of a function wrapped in cudagraphify_impl"
    id: int


@dataclasses.dataclass(frozen=True)
class WrappedFunction:
    model: Callable
    static_input_idxs: Sequence[int]
    id: FunctionID


def clear_cublass_cache():
    """
    Cublas keeps a persistent workspace allocation for running matmuls. This poses a problem for
    doing warmup within a CUDAGraph private pool because we do not want persistent allocations from
    one one run to the next. When we begin a new run of a cudagraphs path (generation), all tensors
    from the previous generation are freed. This frees them the the memory pool, but not elsewhere.
    A tensor in the cublas workspace would continue to be in use the workspace but would also get allocated
    in the next run. The memory would be in use in two places.

    To solve this, we clear cublass caches before and after warming up or recording. If a workspace is required
    it will be allocated to the cudagraph private pool and accounted for in the allocator for the duration of the
    program. There is no overhead to this on replay since cudagraphs removes allocation overhead.
    """
    torch._C._cuda_clearCublasWorkspaces()


@contextlib.contextmanager
def clear_cublas_manager():
    "Context manager around clearing cublass caches that will clear on enter and exit"
    clear_cublass_cache()
    try:
        yield
    finally:
        clear_cublass_cache()


class TreeManagerContainer:
    """
    Manages the lifetime of the tree manager. Like `PrivatePool` in cuda caching allocator,
    the tree and its corresponding memory pool should be kept alive as long as any outstanding
    graph or tensor which is an output of a graph remains alive.

    There is a single tree manager container per device.

    The lifecycle of a tree_manager is:
    -  Is constructed, no graph, no fns, no tensors
    -  Tree manager is fetched, resulting in tree manager being allocated
    -  We generate a bunch of functions, calling add_strong_reference
    -  These functions die, calling finalize_reference
    -  When all the functions die, we finalize_tree_manager.

    TODO: in the future, we would like to do the following once storage weak refs land
    -  We look for all the live storages and add references to THOSE
    -  We count as storages die
    -  All the storages are dead, we deallocate the tree manager
    """

    def __init__(self, device_index):
        # This class keeps a strong reference to tree_manager,
        # but upon all other strong references to the tree_manager will reset it to None.
        # We need a strong reference so that we can still access its attributes upon cleanup.
        self.tree_manager: Optional[CUDAGraphTreeManager] = None

        # Number of outstanding references to the current tree manager
        self.live_cudagraphify_fns = 0

        self.device_index = device_index

        # Following two objects are only set in the case that Tensor outputs outlive
        # the cudagraphify_fns. Reference to the Graph is needed to keep the private pool from
        # deallocation.
        self.live_storages_count = 0
        self.graph: Optional[torch.cuda.CUDAGraph] = None

        self.lock = threading.Lock()

    def _finalize_tensor(self):
        with self.lock:
            self.live_storages_count -= 1
            if self.live_storages_count == 0:
                self.graph = None

                # manager was used again after existing cleanup,
                # we shouldnt set it to None
                if self.live_cudagraphify_fns == 0:
                    self.tree_manager = None

    def finalize_cudagraphify_fn(self):
        with self.lock:
            self.live_cudagraphify_fns -= 1
            if self.live_cudagraphify_fns == 0:
                self._finalize_tree_manager()

    def _finalize_tree_manager(self):
        assert self.lock.locked()
        self.tree_manager = None

        # TODO - when issue #91395 is landed, we can set a weakref on
        # storages and trigger a deallocation when all outputs of the
        # cudagraph are dead.

        # live_storages = list(
        #     tree_manager.live_cudagraph_pool_storages_in_curr_execution()
        # )

        # # Maintain reference to graph to keep tensors alive
        # assert len(tree_manager.roots) > 0, "expected at least one use"
        # root = next(tree_manager.get_roots())
        # self.graph = root.graph
        # seen_storages = set()
        # for stor in live_storages:
        #     if stor in seen_storages:
        #         continue
        #     seen_storages.add(stor)
        #     self.live_storages_count += 1
        # .   weakref.finalize(stor, self._finalize_tensor)

    def add_strong_reference(self, fn: Callable):
        with self.lock:
            self.live_cudagraphify_fns += 1

        weakref.finalize(fn, self.finalize_cudagraphify_fn)

    def get_tree_manager(self) -> CUDAGraphTreeManager:
        with self.lock:
            if self.tree_manager is None:
                self.tree_manager = CUDAGraphTreeManager(self.device_index)
            return self.tree_manager


local = threading.local()

# one tree manager per device
local.tree_manager_containers = {}
local.tree_manager_locks = defaultdict(threading.Lock)


# We need to register this as an object that will be copied over as TLS when new
# threads are created in autograd
torch._C._stash_obj_in_tls("tree_manager_containers", local.tree_manager_containers)
torch._C._stash_obj_in_tls("tree_manager_locks", local.tree_manager_locks)


def get_obj(local, attr_name):
    if hasattr(local, attr_name):
        return getattr(local, attr_name)
    else:
        assert torch._C._is_key_in_tls(attr_name)
        return torch._C._get_obj_in_tls(attr_name)


def get_container(device_index: int):
    container_dict = get_obj(local, "tree_manager_containers")
    lock = get_obj(local, "tree_manager_locks")[device_index]

    with lock:
        if device_index not in container_dict:
            container_dict[device_index] = TreeManagerContainer(device_index)

        return container_dict[device_index]


def cudagraphify_impl(model, inputs, static_input_idxs=(), *, device_index: int):
    manager = get_container(device_index).get_tree_manager()
    return manager.add_function(model, inputs, static_input_idxs)


def is_live(weak_ref):
    if weak_ref is None:
        return False
    return weak_ref() is not None


class StorageWeakRefWrapper:
    """
    Wrapper around a storage weak ref of a Tensor will deallocate it upon
    expiration if invoked.
    """

    storage_ref: Optional[StorageWeakRef]

    def __init__(self, tensor: Tensor):
        assert isinstance(tensor, Tensor)
        stor = tensor.untyped_storage()
        self.ref = StorageWeakRef(stor)
        self.data_ptr = stor.data_ptr()

    def __call__(self) -> Optional[StorageWeakRefPointer]:
        if self.ref is None:
            return None

        if self.ref.expired():
            self.ref = None
            return None

        return self.ref.cdata

    def data_ptr(self) -> int:
        "NB: returns the data ptr even if the storage has expired"
        return self.data_ptr()

    def __repr__(self):
        if self.ref is None or self.ref.expired():
            return f"StorageWeakRefWrapper to {self.data_ptr}; dead"
        else:
            return f"StorageWeakRefWrapper to {self.data_ptr}; alive"


def is_cuda_tensor(x):
    return isinstance(x, torch.Tensor) and x.device.type == "cuda"


@contextlib.contextmanager
def _use_cuda_memory_pool_manager(device, existing_graph, mem_pool):
    """
    Context manager to use cuda graph pool for new allocations. If you use this manager
    all cudagraph tensors in use should be reflected in the allocator or they will be overwritten.
    existing_graph should already have been used in a capture, and the mem_pool must already exist.
    """
    with torch.cuda.device(device):
        capture_id = existing_graph.id()
        torch._C._cuda_allocateThreadToPrivatePool(device, mem_pool)
        torch._C._cuda_notifyCaptureBegin(device, capture_id, mem_pool)
        try:
            yield
        finally:
            torch._C._cuda_notifyCaptureAboutToEnd(device, capture_id)
            torch._C._cuda_notifyCaptureEnded(device, capture_id)
            torch._C._cuda_notifyCaptureDestroy(device, mem_pool)


def map_to_ref(t: Optional[Tensor]) -> Optional[StorageWeakRefWrapper]:
    if not isinstance(t, torch.Tensor):
        assert t is None
        return None
    return StorageWeakRefWrapper(t)


# A path index of (depth, offset) indices into a graph that is `depth`` number of nodes from the root
# at graph output offset
PathOutputIndex = Tuple[int, int]

# For each node in the path, for each output, is the output alive
PathLiveness = List[List[bool]]


class CUDAWarmupNode:
    """
    Simplified Wrapper around A CUDA Model that wraps outputs in storage refs and exposes
    apis to get the live storages in the current chain of warmup.

    A CUDAWarmupNode may have either CUDAGraphNode or CUDAWarmupNode as a parent, but may only have
    CUDAWarmupNode as children, because we cannot record or execute with tensors which do not have stable
    memory addresses.

    CUDAWarmupNode and CUDAGraphNode have a number of differences that make it easier to use separate classes.
    - Much of the CUDAGraphNode logic & initialization is based on the tensor properties of first recording. In the
    first instance of warmup, these are not finalized yet.
    - All Inputs to the RecordedFunction must be copied over to the cuda graph memory pool, this is unnecessary in warmup.
    - CUDAWarmup is only used once and so does not need to optimize as much bookkeeping. It is much simpler.

    NB: this class and CUDAGraphNode need to expose `path_live_weakrefs`, `all_outputs_are_dead`, and
    `self.outputs_weakrefs` for compatibility.
    """

    def __init__(
        self,
        wrapped_function: WrappedFunction,
        parent,
        cuda_graphs_pool: Tuple[int, int],
        existing_cuda_graph: torch.cuda.Graph,
        device_index: int,
    ):
        self.wrapped_function = wrapped_function
        self.parent = parent
        self.cuda_graphs_pool = cuda_graphs_pool
        self.outputs_weakrefs: List[Optional[StorageWeakRefWrapper]] = []
        self.existing_cuda_graph = existing_cuda_graph
        self.has_run = False
        self.device_index = device_index

    def run(self, new_inputs):
        assert not self.has_run, "Wrapped function should never be run twice"

        # See: output_is_alias_of_static_inputs below. We should only be returning freshly created
        # storages in path_live_weakrefs.
        existing_path_data_ptrs = {t.data_ptr for t in self.path_live_weakrefs() if t()}
        non_cudagraph_inps = set()
        for inp in new_inputs:
            if inp.untyped_storage().data_ptr() not in existing_path_data_ptrs:
                non_cudagraph_inps.add(inp.untyped_storage().data_ptr())

        if config.triton.debug_cudagraph_trees:
            refs = list(self.path_live_weakrefs())
            check_memory_pool(self.cuda_graphs_pool, refs)

        with torch.cuda.device(
            self.device_index
        ), clear_cublas_manager(), _use_cuda_memory_pool_manager(
            self.device_index, self.existing_cuda_graph, self.cuda_graphs_pool
        ):
            out = self.wrapped_function.model(new_inputs)

        assert len(new_inputs) == 0

        self.outputs_weakrefs.extend(
            [
                map_to_ref(o)
                for o in out
                if o is not None
                and o.untyped_storage().data_ptr() not in non_cudagraph_inps
            ]
        )

        if config.triton.debug_cudagraph_trees:
            out_refs = self.path_live_weakrefs()
            new_storages = [t for t in out_refs if t.data_ptr not in non_cudagraph_inps]
            check_memory_pool(self.cuda_graphs_pool, new_storages)

        return out

    def path_live_weakrefs(self) -> Generator[StorageWeakRefWrapper]:
        "Returns all live storages weakrefs that created by nodes in this path"
        nodes = []
        node = self
        while node:
            nodes.append(node)
            node = node.parent

        for node in reversed(nodes):
            for output in node.outputs_weakrefs:
                if is_live(output):
                    yield output

    def all_outputs_are_dead(self):
        return not list(self.path_live_weakrefs())


class CUDAGraphNode:
    """
    A single recording of a function into a CUDA Graph. Recordings of CUDA Graphs share a single memory pool
    and are structured into a tree, where there is a single recording that can precede it (parent) and multiple
    subsequent recordings that may follow (children). A node will have no parent if it is the first recording
    in a tree; i.e., when it is first recorded, there are no live tensors from a previous recording which
    would force a dependency.

    On first recording, all of the live tensors in the current CUDA Graph Node path will be
    reflected in the corresponding private pool. On subsequent executions, the caching allocator
    is unaffected when the graph is replayed.

    In order to support recording a subsequent cuda graph recording after execution of this graph,
    we checkpoint the state of the memory pool so that it may later be resumed.

    WrappedFunction should have already been warmed up prior to invocation.

    See [setCheckpointPoolState] for further explanation, as well as
    https://user-images.githubusercontent.com/13564/222815509-374f3400-f83d-4f7d-8fa6-4a092b3250bb.png
    """

    def __init__(
        self,
        wrapped_function: WrappedFunction,
        id: GraphID,
        parent: Optional[CUDAGraphNode],
        inputs: List[Tensor],
        cuda_graphs_pool: Tuple[int, int],
        device_index: int,
    ):
        assert isinstance(inputs, (list, tuple))

        self.wrapped_function = wrapped_function
        self.id = id
        self.device = device_index

        # if this is a root parent will be None. use weakref to prevent reference cycle
        self._parent = weakref.ref(parent) if parent is not None else None
        self.cuda_graphs_pool = cuda_graphs_pool

        # A single wrapped function may be recorded multiple times if memory patterns or
        # invariants change from one execution to the next
        self.children: Dict[FunctionID, List[CUDAGraphNode]] = defaultdict(list)

        # we preserve a single reference to executed outputs that is then referenced
        # in children to avoid children having to chase parent pointers in the hot path
        # DO NOT reassign output_weakrefs, only call `clear()`
        # Path is a series of nodes from root to the current node
        self.outputs_weakrefs: List[Optional[StorageWeakRefWrapper]] = []
        self.path_weakrefs: List[List[Optional[StorageWeakRefWrapper]]] = [
            node.outputs_weakrefs for node in self._path_from_root
        ]

        # tensors which are outputs of previous graphs in the tree
        self.cudagraph_managed_idxs: List[int] = [
            idx
            for idx, t in enumerate(inputs)
            if self._is_cuda_graph_recorded_tensor(t)
        ]

        self.static_input_idxs: List[int] = list(
            set(wrapped_function.static_input_idxs) | set(self.cudagraph_managed_idxs)
        )

        self.static_input_data_ptrs: List[int] = [
            (inputs[i].data_ptr() if i in self.static_input_idxs else None)
            for i in range(len(inputs))
        ]

        # When we checkpoint, and free generations, we will be manually freeing the outputs
        # of CUDAGraphNodes. We should not be freeing parameters, not do we need to account for
        # their liveness (they are static), so we need to compute which outputs are aliases of
        # parameters
        self.static_input_storage_ptrs: Set[int] = {
            inputs[i].untyped_storage().data_ptr()
            for i in self.wrapped_function.static_input_idxs
        }
        self.output_is_alias_of_static_inputs: List[int] = []

        # precompute expanded dims to avoid computing in the hot path
        self.expanded_dims: List[List[int]] = [
            get_expanded_dims(x) if idx not in self.static_input_idxs else []
            for idx, x in enumerate(inputs)
        ]

        # For each node in path, which outputs were observed to be live
        # before invoking graph recording, and after graph recording
        self.recorded_liveness_before_graph: List[List[bool]] = []
        self.recorded_liveness_after_graph: List[List[bool]] = []

        # List of Tuples of (depth, output_index) that index into node at depth
        # number of nodes from root and output_index of outputs. Will index into
        # path_weakrefs.
        self.expected_dead_indices_before_graph: List[PathOutputIndex] = []
        self.expected_dead_indices_after_graph: List[PathOutputIndex] = []

        # all live indices after graph recording
        self.live_indices_after_graph: List[PathOutputIndex] = []

        if self.parent is not None:
            previous_liveness = self.parent.recorded_liveness_after_graph
            curr_liveness = self._get_liveness(self.path_weakrefs)

            different_indices = self._get_different_indices(
                previous_liveness, curr_liveness
            )

            self.recorded_liveness_before_graph = curr_liveness
            self.expected_dead_indices_before_graph = different_indices

        recording_inputs = self._allocate_recording_inputs(inputs)

        # graph used for recording model invocation
        self.graph = torch.cuda.CUDAGraph()

        # we allocate non-static inputs within the same memory pool as the CUDAGraph
        # which we will record the model with. For memory efficiency, it is important
        # to reclaim the input memory when the inputs are no longer live. To accomplish this,
        # we record the metadata needed to reconstruct the inputs at their correct memory location,
        # but do not keep them live during the cuda graph recording.
        self.non_static_inputs_metadata = [
            self._tensor_metadata(x) if idx not in (self.static_input_idxs) else None
            for idx, x in enumerate(recording_inputs)
        ]

        stream = torch.cuda.Stream()

        # on the first invocation, return the first recorded outputs, because their memory
        # is correctly accounted for in the CUDAGraphs caching allocator, so on subsequent cudagraph
        # recording we are tracing with a valid caching allocator state
        self.recording_outputs = self._record(
            wrapped_function.model, stream, recording_inputs
        )

        self.outputs_metadata = []

        # As with inputs, we do not want to keep the outputs permanently alive because that would prevent
        # their memory being reclaimed in subsequent cuda graph recordings. We record the tensor metadata
        # needed to reconstruct instead.
        for out in self.recording_outputs:
            if isinstance(out, torch.Tensor):
                self.outputs_metadata.append(
                    self._tensor_metadata(out, ignore_storage_offset=False)
                )
            else:
                assert out is None
                self.outputs_metadata.append(None)

        # initialized on first run
        self.checkpointed_caching_state: Optional[AllocatorState] = None

    def run(self, new_inputs):

        if config.triton.debug_cudagraph_trees:
            self.debug_check_invariants_before_invocation()

        assert len(self.static_input_data_ptrs) == len(new_inputs)

        storage_cache = {}
        for idx, data_ptr in enumerate(self.static_input_data_ptrs):
            if idx in self.cudagraph_managed_idxs:
                continue
            if data_ptr is not None:
                assert data_ptr == new_inputs[idx].data_ptr()
            else:
                dst = self._reconstruct_from_tensor_metadata(
                    self.non_static_inputs_metadata[idx], storage_cache
                )
                src = new_inputs[idx]
                expanded_dims = self.expanded_dims[idx]

                dst = index_expanded_dims(dst, expanded_dims)
                src = index_expanded_dims(src, expanded_dims)
                # TODO - one jit kernel across multiple inputs
                dst.copy_(src)

        new_inputs.clear()
        self.graph.replay()

        # outputs is not None on first execution
        if self.recording_outputs is not None:
            outputs = self.recording_outputs
            self.recording_outputs = None
            self._add_first_outputs(outputs)

            return outputs

        outputs = [
            (
                self._reconstruct_from_tensor_metadata(metadata, storage_cache)
                if metadata
                else None
            )
            for metadata in self.outputs_metadata
        ]

        self._add_replayed_outputs(outputs)
        self.debug_check_invariants_after_invocation()

        return outputs

    def all_outputs_are_dead(self):
        "All outputs of the path from this node to its root are dead"
        for depth, output_index in self.live_indices_after_graph:
            try:
                if is_live(self.path_weakrefs[depth][output_index]):
                    return False
            except Exception as e:
                breakpoint()
                raise

        return True

    def _record(self, model, stream, inputs):
        "Record the model"

        if config.triton.debug_cudagraph_trees:
            # need to use parent live weakrefs because live_indices isnt set yet
            memory = (
                [] if self.parent is None else list(self.parent.path_live_weakrefs())
            )
            memory += [
                StorageWeakRefWrapper(elem)
                for i, elem in enumerate(inputs)
                if i not in self.wrapped_function.static_input_idxs
            ]
            check_memory_pool(self.cuda_graphs_pool, memory)

        with torch.cuda.device(self.device), clear_cublas_manager(), torch.cuda.graph(
            self.graph, stream=stream, pool=self.cuda_graphs_pool
        ):
            static_outputs = model(inputs)

        # running model should reclaim memory
        assert len(inputs) == 0

        if not isinstance(static_outputs, (list, tuple)):
            static_outputs = (static_outputs,)

        return static_outputs

    def _add_first_outputs(self, outputs):
        "Add the outputs from the first invocation of the node and set up metadata"

        # getting liveness before we have added the outputs to path, so the length
        # of the two lists is equal
        prev_liveness = self.recorded_liveness_before_graph
        curr_liveness = self._get_liveness(self.path_weakrefs)

        delta = self._get_different_indices(prev_liveness, curr_liveness)
        self.expected_dead_indices_after_graph = delta

        assert len(self.outputs_weakrefs) == 0
        for i, o in enumerate(outputs):
            self.output_is_alias_of_static_inputs.append(
                o is not None
                and o.untyped_storage().data_ptr() in self.static_input_storage_ptrs
            )

        self._add_replayed_outputs(outputs)
        self.recorded_liveness_after_graph = self._get_liveness(self.path_weakrefs)

        self.checkpointed_caching_state = torch._C._cuda_getCheckpointState(
            self.device, self.cuda_graphs_pool
        )

        # now, get liveness with outputs added
        for depth in range(len(self.path_weakrefs)):
            for output_index in range(len(self.path_weakrefs[depth])):
                if is_live(self.path_weakrefs[depth][output_index]):
                    self.live_indices_after_graph.append((depth, output_index))

        self.debug_check_invariants_after_invocation()
        if config.triton.debug_cudagraph_trees:
            check_memory_pool(self.cuda_graphs_pool, list(self.path_live_weakrefs()))

    def _add_replayed_outputs(self, outputs):
        self.outputs_weakrefs.clear()
        for out, is_alias in zip(outputs, self.output_is_alias_of_static_inputs):
            self.outputs_weakrefs.append(map_to_ref(out) if not is_alias else None)

    @property
    def parent(self):
        "unwraps the weakref to _parent"
        return self._parent() if self._parent is not None else None

    @property
    def _path_to_root(self):
        "Returns all nodes in the path starting at self and ending at root"
        node = self
        while node:
            yield node
            node = node.parent

    @property
    def _path_from_root(self):
        "Returns all nodes in the path starting at the root and ending at self"
        nodes = reversed(list(self._path_to_root))
        for node in nodes:
            yield node

    def _is_cuda_graph_recorded_tensor(self, t: torch.Tensor):
        "Is this tensor an output of a node in this path"
        for output_refs in self.path_weakrefs:
            for storage_weak_ref in output_refs:
                if storage_weak_ref is None:
                    continue
                # dont need to check liveness of storage since the cuda graph managed
                # memory is never released.
                data_ptr = storage_weak_ref.data_ptr
                if t.untyped_storage().data_ptr() == data_ptr:
                    return True

        return False

    @staticmethod
    def _check_liveness(indices: List[PathOutputIndex], output_refs: List[List[bool]]):
        "Check that all of the indices specified are dead references"
        for depth, output_index in indices:
            if output_refs[depth][output_index]() is not None:
                return False
        return True

    def add_child(self, function_id: FunctionID, node: CUDAGraphNode):
        "Adds node as a a child of self"
        self.children[function_id].append(node)

    @staticmethod
    def _get_different_indices(
        prev: List[List[bool]], curr: List[List[bool]]
    ) -> List[PathOutputIndex]:
        "Find indices where the two lists differ."
        dead_indices = []
        assert len(prev) <= len(curr)
        for i, (outputs1, outputs2) in enumerate(zip(prev, curr)):
            assert len(outputs1) == len(outputs2)
            for j, (output1, output2) in enumerate(zip(outputs1, outputs2)):
                if output1 != output2:
                    dead_indices.append((i, j))

        return dead_indices

    @staticmethod
    def _get_liveness(
        weakrefs: List[List[Optional[StorageWeakRefWrapper]]],
    ) -> List[List[bool]]:
        "Maps weakrefs to true if the reference is alive and false otherwise"
        if len(weakrefs) == 0:
            return []

        return [pytree.tree_map(is_live, outputs) for outputs in weakrefs]

    def debug_assert_invariants(
        self, expected_liveness: List[List[bool]], newly_dead: List[PathOutputIndex]
    ):
        if not config.triton.debug_cudagraph_trees:
            return

        for i, node in enumerate(self._path_from_root):
            assert self.path_weakrefs[i] is node.outputs_weakrefs

        for depth, outputs_liveness in enumerate(expected_liveness):
            for output_idx, output_liveness in enumerate(outputs_liveness):
                # tensor can die early, but it can't be alive when it should be dead
                assert output_liveness or not self.path_weakrefs[depth][output_idx]()

        for depth, output_index in newly_dead:
            assert not self.path_weakrefs[depth][output_index]()

    def debug_check_invariants_before_invocation(self):
        self.debug_assert_invariants(
            self.recorded_liveness_before_graph, self.expected_dead_indices_before_graph
        )

    def debug_check_invariants_after_invocation(self):
        self.debug_assert_invariants(
            self.recorded_liveness_before_graph, self.expected_dead_indices_after_graph
        )

    def data_ptrs_dead_since_invocation(self) -> List[int]:
        """
        Since this node was invoked, return data ptrs of all tensor outputs that have died
        in the current executing tree path.
        """
        curr_liveness = self._get_liveness(self.path_weakrefs)
        _get_different_indices = self._get_different_indices(
            self.recorded_liveness_after_graph, curr_liveness
        )

        path = list(self._path_from_root)
        ptrs_to_deallocate = []
        for (depth, output_index) in _get_different_indices:
            ptrs_to_deallocate.append(
                path[depth].outputs_metadata[output_index]["data_ptr"]
            )

        return ptrs_to_deallocate

    def path_live_weakrefs(self) -> Generator[StorageWeakRefWrapper]:
        "Returns all live storages weakrefs that created by nodes in this path"
        for (i, j) in self.live_indices_after_graph:
            out = self.path_weakrefs[i][j]
            if is_live(out):
                yield out

    def clear_path_outputs(self):
        "Clear the output lists of all nodes in the path"
        for li in self.path_weakrefs:
            li.clear()

    @staticmethod
    def _tensor_metadata(x, ignore_storage_offset=True):
        assert isinstance(x, torch.Tensor)
        # We ignore the storage offset for inputs, but not for outputs
        # TODO: - should we make the storage resizable ?
        return {
            "nbytes": x.untyped_storage().nbytes(),
            "data_ptr": x.untyped_storage().data_ptr(),
            "size": x.shape,
            "stride": x.stride(),
            "dtype": x.dtype,
            "device": x.device,
            "storage_offset": x.storage_offset() if not ignore_storage_offset else 0,
        }

    @staticmethod
    def _reconstruct_from_tensor_metadata(
        metadata: Dict[str, Any], storage_cache: Dict[int, torch.Storage]
    ) -> Tensor:
        s = storage_cache.get(metadata["data_ptr"], None)
        if s is None:
            s = torch._C._construct_storage_from_data_pointer(
                metadata["data_ptr"], metadata["device"], metadata["nbytes"]
            )
        t = torch.empty([0], device=metadata["device"], dtype=metadata["dtype"])
        t.set_(
            source=s,
            storage_offset=metadata["storage_offset"],
            size=metadata["size"],
            stride=metadata["stride"],
        )
        return t

    def _allocate_recording_inputs(self, inputs):
        "Allocate inputs for non static, non cudagraph managraphed managed tensors in the memory pool"
        inps_alloc_graph = torch.cuda.CUDAGraph()
        torch.cuda.synchronize()
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        recording_inputs = []

        with warnings.catch_warnings(record=True), torch.cuda.device(
            self.device
        ), torch.cuda.graph(
            inps_alloc_graph,
            pool=self.cuda_graphs_pool,
            stream=stream,
        ):
            for i, inp in enumerate(inputs):
                if i not in self.static_input_idxs:
                    recording_inputs.append(static_input(inp))
                else:
                    recording_inputs.append(inp)

        return recording_inputs

    def check_invariants(self, inputs: List[Tensor]) -> bool:
        """
        Checks if this node can be run. The same pattern of tensor liveness and tensors
        managed in the cudagraph private pool must remain stable.
        """

        # previously managed data pointers remain stable
        for idx in self.cudagraph_managed_idxs:
            if inputs[idx].data_ptr() != self.static_input_data_ptrs[idx]:
                return False

        if not self._check_liveness(
            self.expected_dead_indices_before_graph, self.path_weakrefs
        ):
            return False

        # the cudagraph managed tensors which died upon recording must also die upon
        # this invocation. it is too late to check after we've replayed the graph,
        # because we would have already written over their memory.
        for idx in self.cudagraph_managed_idxs:
            inputs[idx] = None

        check(
            self._check_liveness(
                self.expected_dead_indices_after_graph, self.path_weakrefs
            ),
            lambda: "TODO: graph recording observed an input tensor deallocate during graph "
            " recording that did not occur during replay. Please file an issue.",
        )
        return True

    def num_descendants(self) -> int:
        "Total number of descendents of this node"
        num_desc = 0
        for children in self.children.values():
            for child in children:
                num_desc += 1
                num_desc += child.num_descendants()
        return num_desc


def get_cudagraph_segments(pool_id):
    segments = torch.cuda.memory_snapshot()
    return [segment for segment in segments if segment["segment_pool_id"] == pool_id]


def check_memory_pool(pool_id, live_storages_ptrs: List[StorageWeakRefWrapper]):
    assert all([isinstance(elem, StorageWeakRefWrapper) for elem in live_storages_ptrs])
    gc.collect()

    unique_storages = {stor.data_ptr for stor in live_storages_ptrs if stor()}
    segments = get_cudagraph_segments(pool_id)

    for segment in segments:
        addr = segment["address"]
        for block in segment["blocks"]:
            if block["state"] == "active_allocated":
                check(
                    addr in unique_storages,
                    lambda: f"{addr} allocated but not in live storages",
                )
                unique_storages.remove(addr)

            addr += block["size"]

    check(
        len(unique_storages) == 0,
        lambda: f"These storage data ptrs are not allocated in pool {pool_id} but should be {unique_storages}",
    )


class ExecutionState(Enum):
    """
    Represents the state of the CUDAGraph Tree. Will be None if there is no live current memory allocated
    in the cuda graph pool. Otherwise will reflect the state of the most recently executed node.
    """

    NONE = auto()
    WARMUP = auto()
    RECORDING = auto()
    EXECUTION = auto()


class CUDAGraphTreeManager:
    """
    Groups individual recordings or executions of cuda graphs into a tree of recordings,
    and checks required invariants, and manages warmups of graphs.

    When graphs are recorded in the same tree, it enforces subsequent execution
    to follow the same order and have the same output tensor livespans. To remove
    unnecessary coupling of cuda graphs (and additional imposed invariants),
    the tree manager will end a currently recording tree whenever it is valid - when
    the memory pool no longer has any live allocations.

    We ignore outputs from a previous generation that correspond to prior model outputs.
    Currently this is hardcoded `GenerationTracker.generation` tracked in torch dynamo.
    # TODO: make generation increment configurable, warn on overwrite.

    We run graph warmups in the cudagraph memory pool and return the result on the first invocation
    of a function. For many models it is important to reclaim activations as you run the backward.
    If we were to warm up the model and keep an extra copy of the inputs around to subsequently
    use for recording, we would incur a memory penalty. Additionally, if we are part way through training
    your model and need to recompile, memory will be allocated to the cuda graph pool, so we run this
    warmup run in the cuda graph memory pool. As for recording, warm up needs the state of live tensors
    to be accurately reflected so we checkpoint the allocator state if we need to warm up following graph
    replay.
    """

    def __init__(self, device_index: int):
        # roots are functions which have no dependencies on an other node. I.e.,
        # when they are first invoked, none of their inputs are outputs are outputs
        # of another node, nor are there any live outputs of another node whose
        # liveness would create a dependency.
        self.roots: Dict[FunctionID, List[CUDAGraphNode]] = defaultdict(list)

        # mapping from function id to wrapped function
        self.ids_to_funcs: Dict[FunctionID, WrappedFunction] = {}

        self.warmed_up_functions: Set[FunctionID] = set()

        with torch.cuda.device(device_index):
            self.cuda_graphs_thread_pool = torch.cuda.graph_pool_handle()
            # Keeps Memory Pool Alive
            self.graph = torch.cuda.CUDAGraph()

            torch.cuda.synchronize()
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())

            self.cuda_graphs_thread_pool = torch.cuda.graph_pool_handle()

            with warnings.catch_warnings(record=True), torch.cuda.graph(
                self.graph,
                pool=self.cuda_graphs_thread_pool,
                stream=stream,
            ):
                pass

        self.graph_counter = itertools.count(0)
        self.func_counter = itertools.count(0)

        # whether we the current node is in a state of warmup, recording, execution. If
        # there is no current node the state will be ExecutionState.None.
        self.path_state = ExecutionState.NONE
        self.device_index = device_index

        # the most recently invoked cudagraph wrapping of a function. Will be None
        # when there is no output from a previous recording or execution whose memory
        # we need to respect in the cuda caching allocaton. If you incremented generation,
        # this will also be none, as ignore those allocations.
        self.current_node: Optional[CUDAGraphNode] = None

        # current generation of cudagraph invocations. when torch.compile is run
        # we increment the current generation. are willing to ignore live outputs
        # of a previous generation in checking liveness.
        self.current_gen: int = -1

        # number of instances we are in execution and failed to match to an
        # existing child
        self.debug_fail_counter = 0
        # number of instances we had to checkpoint the function
        self.debug_checkpointing_counter = 0

    def run(self, new_inputs: List[Tensor], function_id: FunctionID):

        # we will try to end the current execution lazily, since
        # we dont want to do unnecessary checking of the existing outputs
        # on the hot path, but both recording and warmup only happen once
        # so we check up front
        if self.in_recording:
            self.try_end_curr_recording()

        if self.in_warmup:
            self.try_end_curr_warmup()

        # warming up a function and subsequentally recording may use different memory addresses
        # because both depend on the state of the caching allocator. if we warm up graph A,
        # then warm up graph B and make more allocations, the subsequent recording of A will not
        # necessarily use the same addresses as in the warm up. Thus any warm up of a node can only
        # be followed by warm up runs.
        if (
            not (
                function_id in self.warmed_up_functions
                or config.triton.skip_cudagraph_warmup
            )
        ) or self.in_warmup:
            self.warmed_up_functions.add(function_id)
            # If we are in the middle of executing cuda graphs, then we need to checkpoint memory state.
            # Both Recording and Warmup will be reflected in the allocator and dont need changes
            if self.path_state == ExecutionState.EXECUTION:
                self.apply_checkpoint_execution_state_in_allocator()

            return self.run_eager(new_inputs, function_id)

        child_nodes = (
            self.roots if self.current_node is None else self.current_node.children
        )

        if not self.in_recording:
            for child in child_nodes[function_id]:
                # here we are checking memory consistency between recording and execution,
                # as well as things like stability of tensor locations, etc
                # and other
                if child.check_invariants(new_inputs):
                    return self.execute_node(child, new_inputs)

            # now that we know the new function can't be run as a child of the
            # current node, if it is a root, try to end the current execution.
            # as noted above, we want to do this lazily to avoid having to
            # check all existing outputs
            if self.current_node is not None and function_id in self.roots:
                self.try_end_curr_execution()

                # run again to hit the root matching case which must succeed
                if self.current_node is None:
                    return self.run(new_inputs, function_id)

            # at this point, we necessarily will do a new recording
            self.debug_fail_counter += 1

            self.try_end_curr_execution()
            if self.current_node is not None:
                self.apply_checkpoint_execution_state_in_allocator()

        # now, we are in a recording state !
        return self.record_function(new_inputs, function_id)

    def record_function(self, new_inputs, function_id) -> List[Optional[Tensor]]:
        torch.cuda.synchronize()
        node = CUDAGraphNode(
            self.ids_to_funcs[function_id],
            self.new_graph_id(),
            self.current_node,
            new_inputs,
            self.cuda_graphs_thread_pool,
            self.device_index,
        )
        if self.current_node is None:
            self.roots[function_id].append(node)
        else:
            self.current_node.add_child(function_id, node)
        self.current_node = node
        self.path_state = ExecutionState.RECORDING
        self.update_generation()
        torch.cuda.synchronize()
        return node.run(new_inputs)

    def execute_node(self, node: CUDAGraphNode, new_inputs) -> List[Optional[Tensor]]:
        self.current_node = node
        self.path_state = ExecutionState.EXECUTION
        self.update_generation()
        return node.run(new_inputs)

    def run_eager(self, new_inputs, function_id: FunctionID):
        # this is only stored on current node, because when we start a new path,
        # we will deallocate it
        node = CUDAWarmupNode(
            self.ids_to_funcs[function_id],
            self.current_node,
            self.cuda_graphs_thread_pool,
            self.graph,
            self.device_index,
        )
        self.current_node = node
        self.path_state = ExecutionState.WARMUP
        self.update_generation()
        return node.run(new_inputs)

    def update_generation(self):
        self.current_gen = self.get_curr_generation()

    def new_graph_id(self) -> GraphID:
        return GraphID(next(self.graph_counter))

    def new_func_id(self) -> FunctionID:
        return FunctionID(next(self.func_counter))

    def add_function(self, model, inputs, static_input_idxs) -> Callable:
        id = self.new_func_id()
        self.ids_to_funcs[id] = WrappedFunction(
            model, remove_unaligned_input_idxs(inputs, static_input_idxs), id
        )
        fn = functools.partial(self.run, function_id=id)

        # container needs to set clean up when fn dies
        get_container(self.device_index).add_strong_reference(fn)
        return fn

    @property
    def in_recording(self):
        return self.path_state == ExecutionState.RECORDING

    @property
    def in_warmup(self):
        return self.path_state == ExecutionState.WARMUP

    def get_roots(self) -> Generator[CUDAGraphNode]:
        for nodes in self.roots.values():
            for node in nodes:
                yield node

    @property
    def current_node(self):
        return self._current_node

    @current_node.setter
    def current_node(self, value):
        self._current_node = value
        if value is None:
            self.path_state = ExecutionState.NONE

    @staticmethod
    def get_curr_generation() -> int:
        return GenerationTracker.generation

    def try_end_curr_recording(self) -> None:
        """
        Check if the current recording can be terminated, either because all outputs of the
        previously recorded node are dead or because it was executed in a different
        generation. Will set current_node to None and in_recording to False if successful.
        """
        assert self.in_recording
        assert self.current_node is not None

        # multiple invocations, allow overwriting the previous generation
        if self.current_gen != self.get_curr_generation():
            self.dealloc_current_path_weakrefs()
            self.clear_current_node_outputs_and_set_to_none()
            return

        if self.current_node.all_outputs_are_dead():
            self.clear_current_node_outputs_and_set_to_none()
            return

    def try_end_curr_execution(self) -> None:
        """
        Check if the current executing node can be terminated, either because all outputs of the
        previously executed node are dead or because it was executed in a different generation.
        Will set current_node to None if successful.
        """

        assert not self.in_recording
        if self.current_node is None:
            return

        if self.current_gen != self.get_curr_generation():
            self.clear_current_node_outputs_and_set_to_none()
            return

        if self.current_node.all_outputs_are_dead():
            self.clear_current_node_outputs_and_set_to_none()

    def try_end_curr_warmup(self):
        if self.current_gen != self.get_curr_generation():
            self.dealloc_current_path_weakrefs()
            self.current_node = None
            return

        if self.current_node.all_outputs_are_dead():
            self.current_node = None
            return

    def dealloc_current_path_weakrefs(self):
        # TODO: we could also allow the these weak refs to continue to be allocated,
        # but that adds some complications.
        for t in self.current_node.path_live_weakrefs():
            if t():
                torch._C._free_And_Remove_DeleterFn(t())

    def clear_current_node_outputs_and_set_to_none(self):
        self.current_node.clear_path_outputs()
        self.current_node = None

    def apply_checkpoint_execution_state_in_allocator(self):
        """
        Checkpoint the current execution state in the caching allocator so that
        additional cudagraph recordings can be made respecting existent live storages.
        """
        self.debug_checkpointing_counter += 1
        state = self.current_node.checkpointed_caching_state
        device = self.current_node.device
        assert state is not None and device is not None

        # currently we deallocate on instead of allowing stale recordings
        stale_storages = []
        live_storages_wrappers = list(self.current_node.path_live_weakrefs())

        live_storages_weak_refs = [t() for t in live_storages_wrappers]
        ptrs_to_deallocate = self.current_node.data_ptrs_dead_since_invocation()
        torch._C._cuda_setCheckpointPoolState(
            device, state, stale_storages, live_storages_weak_refs
        )

        for ptr in ptrs_to_deallocate:
            torch._C._cuda_cudaCachingAllocator_raw_delete(ptr)

        # Now the live blocks should be exactly equal to the live storages in private pool
        if config.triton.debug_cudagraph_trees:
            check_memory_pool(self.cuda_graphs_thread_pool, live_storages_wrappers)

    def live_cudagraph_pool_storages_in_curr_execution(
        self,
    ) -> List[StorageWeakRefPointer]:
        if self.current_node is None:
            return []
        # explicitly ignoring previous recorded outputs from past path
        return [t() for t in self.current_node.path_live_weakrefs()]
