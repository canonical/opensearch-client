# ISM policies

The `os-cli` tool and `opensearch_client` library support the management of
Index State Management (ISM) policies.

An ISM policy describes the states an index moves through and the actions ISM
takes in each, such as rolling the index over to a new one or deleting it. A
policy is stored in the cluster once, and ISM then runs it for each index the
policy is attached to. Storing a policy does not attach it to any index.

## Applying a policy to both new and existing indices

OpenSearch has two ways to attach a policy to indices, and a third way to move
an index from one policy to another:

- An `ism_template` block in the policy lists index-name patterns. When an index
  is created and its name matches a pattern, ISM attaches the policy to it. ISM
  checks only when an index is created, so the block never reaches an index that
  existed before the policy was stored.
- The add API (`POST _plugins/_ism/add/<index>`) attaches the policy to indices
  that exist now. It only acts on an index that has no policy. It refuses an
  index that already has one, and lists each refused index in a response whose
  status is still 200.
- The change policy API (`POST _plugins/_ism/change_policy/<index>`) moves
  indices that already have a policy to another policy, or to a newer version of
  the same one. The move is not immediate: ISM queues it, and each index
  switches once it finishes the actions of the state it is in.

Applying a policy to both new and existing indices therefore takes three steps.
First, store the policy with an `ism_template` block, so that indices created
from then on get it. Second, attach it to the indices that already exist. Third,
check the result with the explain API (`GET _plugins/_ism/explain/<index>`),
which reports, for each index, the policy that manages it, or none.

The library methods for these calls are:

| OpenSearch API                             | Library method                            |
| ------------------------------------------ | ----------------------------------------- |
| `PUT _plugins/_ism/policies/<id>`          | `set_ism_policy`                          |
| `POST _plugins/_ism/add/<index>`           | `attach_ism_policy`                       |
| `POST _plugins/_ism/change_policy/<index>` | `attach_ism_policy`, unless `force=False` |

`attach_ism_policy` covers both the add and the change policy APIs, so that
attaching a policy has the result its name suggests: every matching index ends
up with the policy. It first asks the explain API which matching indices already
have a policy. It sends the others to the add API, and the ones that already
have a policy to the change policy API, so they are moved and switch later as
described above.

The explain API lags behind: an index attached to a policy shows no policy until
ISM's next background sweep, which runs every 10 minutes by default
(`plugins.index_state_management.coordinator.sweep_period`). The same can happen
to an index that an `ism_template` attached when a rollover created it. The add
API refuses such an index, so `attach_ism_policy` also sends every index the add
API refused to the change policy API, whatever the reason. An index refused for
another reason is refused again, and both reasons are reported.

Pass `force=False` to leave indices that already have a policy on it instead of
moving them. Either way, any index that did not get the policy makes the result
a failure that names the index and the reason.

`os-cli cluster ism-policy` and `os-cli cluster set ism-policy` show and store
policies from the command line.

## Policies that roll indices over

A rollover creates a new index and sends new writes to it, so one stream of data
is spread over a series of indices, such as `logs-000001` and `logs-000002`.
Writers do not name these indices. They write to an alias, and the alias marks
one of its indices as the write index: the one that receives the writes.

When ISM runs a policy's `rollover` action on an index, it needs to know which
alias to roll over, and it reads the alias name from the index setting
`index.plugins.index_state_management.rollover_alias`. The rollover works only
if that setting names an alias and the index is the current write index of that
alias. Otherwise the action fails. Once the retries in the policy are used up,
ISM marks the index as failed and stops moving it through the policy until the
retry API (`POST _plugins/_ism/retry/<index>`) is called for it.

New indices get the setting from the index template that creates them. An index
created before the template set it, or with the wrong alias, needs the setting
changed directly through the update settings API (`PUT <index>/_settings`, or
`update_index_settings` in the library) before the policy is attached to it.
`get_index_settings` with `flat_settings=True` reads it back as one dotted key.
