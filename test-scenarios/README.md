# Test scenarios

## Advertised routes

The advertised-route scenarios exercise IPv4 load-balancer route
advertisement.  They measure initial centralized population, the transition
to distributed advertisement, sparse load-balancer option changes, bulk and
sparse monitor transitions, an unchanged controller recompute, a Southbound
database reconnect, and cleanup.  Each convergence phase checks
Service_Monitor state, Southbound Advertised_Route rows, and kernel routes.
Route row identities must remain stable while their desired state is
unchanged.  Sparse option changes must preserve unaffected route rows.

The supplied profiles are:

| Configuration | Mode | Purpose |
| --- | --- | --- |
| `ovn-advertised-route-low-scale.yml` | distributed | 8-row CI check |
| `ovn-advertised-route-distributed-1000.yml` | distributed | 8000-row scale run |
| `ovn-advertised-route-centralized-1000.yml` | centralized | 1000-LB centralized control |
| `ovn-advertised-route-opt-out-1000.yml` | disabled | 1000-LB feature opt-out control |

The scale profiles use the same 1000 LBs, 4 backends per LB, and 4000
Service_Monitor rows.  In the supplied topology, each advertiser has one
advertising logical router port.  A centralized LB produces one
Advertised_Route per advertising port and VIP.  A distributed LB produces one
route per advertising port, VIP and backend.  The resulting route counts are
2000 and 8000 respectively with the supplied two-advertiser topology.

Run the low-scale check after functional changes:

```
./do.sh run test-scenarios/ovn-advertised-route-low-scale.yml \
    advertised-route-low-scale
```

To measure the effect on centralized load balancers, run
`ovn-advertised-route-centralized-1000.yml` several times on the same host
with the OVN revision immediately before the change and with the candidate
revision.  This is an enabled-feature comparison and can include work needed
to maintain forwarding routes.  Use `ovn-advertised-route-opt-out-1000.yml`
as the no-impact control without enabling dynamic routing or setting
load-balancer route advertisement options.  Compare topology mutation,
monitor convergence, unchanged recompute and cleanup counters.

The distributed profile creates the topology in centralized mode first.  It
then measures the full transition to distributed advertisement and repeated
changes to a small LB subset.  These phases cover the northd paths that change
router distribution mode and update per-backend route rows.  Bulk and sparse
monitor phases cover controller reconciliation when backend health changes.
The reconnect phase covers controller monitor resynchronization without
changing the desired route state.

For regression testing, run each revision at least ten times on the same
otherwise idle host.  Reduce repeated measurements of a counter within one
run to their median, then compare the per-run samples with a statistical tool
such as `benchstat`.

Treat a result as a possible regression when it is statistically significant,
at least 10 percent slower, and at least 0.1 seconds slower.  Confirm a flagged
result with the revision order reversed before drawing a conclusion.  A failed
state or row-count check is a correctness failure regardless of timing.  Keep
the collected process statistics when investigating a change in wall-clock
results.  For process CPU, subtract the first `cpu_seconds` sample from the
last sample for each PID, sum the deltas by process instance, and compare the
per-run totals.  Sampled CPU percentage is useful for finding bursts but not
for comparing total work.  The CI low-scale test checks correctness, but
shared CI hosts should not enforce performance thresholds.
