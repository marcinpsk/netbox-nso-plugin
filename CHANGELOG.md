# Changelog

<!-- version list -->

## v1.0.0 (2026-08-23)

### Bug Fixes

- **adapter-client**: Harden the HTTP error envelope
  ([`43efeeb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/43efeeb0793637cdb4dbbdc0b1ad2ff598a36c98))

- **adapter-client**: Key transport instrumentation on the pool object
  ([`146a70c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/146a70c8b4c18b6770ba463f44c448776bf95abf))

- **adapter-client**: Make the transport abort sticky across late connects
  ([`296114c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/296114c2e0b21b8468ea545fd7c0587cc50f62ab))

- **adapter-client**: Raise the typed refusal for a non-JSON settlement feed
  ([`e450c68`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e450c68d388cdf8fc52d5e339f20c894b6adcf15))

- **adapter-client**: Refuse a wire-body capture that carries no JSON
  ([`1672de7`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1672de7f2b59599816f09677d7e16b46266f43fa))

- **adapter-client**: Validate job payloads at the client boundary
  ([`a9cefb3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/a9cefb3fd8de3cafb4a968ba07fde7f4c6da3048))

- **adapter-client**: Weakly track instrumented connections
  ([`b81bcba`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b81bcbaa99677baf13a45b93063796f21a9c4402))

- **commands**: Validate --scope against the delivery registry
  ([`78f58b9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/78f58b90ad7ebacf01dbc31c422d22c00f31384a))

- **delivery**: Keep the waiter wake-up ahead of the import
  ([`dbc782d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/dbc782d3ae59f733aee10d0b93203c77c534ad96))

- **delivery**: Never let teardown decide a push that already answered
  ([`9ad9259`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9ad9259a375b399eb56b26c1768b85953b4220e2))

- **delivery**: Reject a deletion mark on a backfill-only send
  ([`4c39ce3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4c39ce34a3de2648ac28ce15e4899e1746cabe2a))

- **delivery**: Resolve the push function at call time
  ([`5725183`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5725183894fb9867934a2f2679a8539edc25d96e))

- **drain**: Clear the acknowledged lineage in one transaction
  ([`bc8de1f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bc8de1f81f06290f21edcf3626fbb2cf4beeff1a))

- **intent-drift**: Compare-and-set the rollback on the armed status
  ([`848dd84`](https://github.com/marcinpsk/netbox-nso-plugin/commit/848dd84800bffb323f345611ae53628dec2b7b5c))

- **intent-drift**: Keep a failed rollback local to its own device
  ([`c78b4d0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c78b4d06068634e42bb5a6a2b8d420e1700c1555))

- **ip-autoassign**: Report and roll back a failed push schedule
  ([`ad1b3ff`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ad1b3ff7e8b64ba192f6b4209148ad105d9fdd41))

- **jobs**: Check the free-form interior of a job's error detail
  ([`c4cd1d4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c4cd1d48a6d7e80b5334f8f13019925519a29529))

- **jobs**: Check the free-form interior of a job's result
  ([`bf46267`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bf46267a4577c0483977e4c6f138c505b1d5baba))

- **jobs**: Contain a drain error so the settlement sweep still runs
  ([`3993492`](https://github.com/marcinpsk/netbox-nso-plugin/commit/399349226308ac07feb6b6aab0885970642f4d2c))

- **migrations**: Keep the push sequence on a 0018 rollback
  ([`bc8a50e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bc8a50ed9e49e21960b7dcf4b5f479e10a4702cb))

- **reconcile**: Clamp negative outcome counts to zero
  ([`3943a42`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3943a422198f81da394b9f50fc964ba0f8b78f3f))

- **reconcile**: Read apply_failed through the counts helper
  ([`79d5541`](https://github.com/marcinpsk/netbox-nso-plugin/commit/79d5541ef2e35a013adf5e4689c3db77ba5a8136))

- **reconcile**: Reject boolean outcome counts
  ([`2f7bfc0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2f7bfc051fbfc4d046261a7f09d0d23ded53a593))

- **release**: Bootstrap uv inside the PSR action container
  ([`2135e1b`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2135e1be57d70e4e4aa775c03967c9e4e9d07193))

- **release**: Pin the bootstrapped uv and validate before publishing refs
  ([`958bc63`](https://github.com/marcinpsk/netbox-nso-plugin/commit/958bc638cb33a2d6ae3860c0eba9067f7db7b273))

- **release**: Pin the build job's uv to the bootstrap version
  ([`af56671`](https://github.com/marcinpsk/netbox-nso-plugin/commit/af56671c605f25fcbc6f9efd844e8a1f54ae2702))

- **resync**: Report partially restored route rows
  ([`27f213e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/27f213ef20e8bd0870b47e258a85ce232900851f))

- **tab**: Correct two device-tab surfaces that misreported what happened
  ([`e7493bb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e7493bbd802e6a95632f92c8f812a338521f063c))

- **tests**: Retire delivered entries in the delivery mixin
  ([`396b1b8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/396b1b81d2dc2a414290b6b30f2363011b181c34))

- **views**: Read the apply deadline through the drain send clock
  ([`8cfa809`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8cfa809deba17c91f9624b0d8d321d5493574ae0))

- **views**: Rebuild the Apply refusal wording instead of serializing the exception
  ([`9d9d4a9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9d9d4a9324d5e568fc41b15d8ca7c0d6d7ea586e))

### Chores

- **ci**: Bump astral-sh/setup-uv in the actions group
  ([`8631903`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8631903ab8cfdd53c3cd6b5a6c3b26d5e1c7a167))

- **deps**: Bump vitest in the js-minor-patch group
  ([`b385f21`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b385f21d83de6f0b6636429f0651e9c2e108badb))

### Code Style

- **resync**: Use the workspace comment punctuation
  ([`19e36a3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/19e36a34d295e8a306139805d9e5959c858f9f6f))

### Continuous Integration

- Pin ruff to the version the commit hook runs
  ([`c2cbef0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c2cbef084f835ccf4c844924606f54276996ed88))

- **release**: Pin the uv version the release job locks with
  ([`f31f9e2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f31f9e208b68a1c386f04ff3b63ea977d4aff97d))

### Documentation

- Drop stale forced-claim wording and pin the bulk-write policy
  ([`853cde9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/853cde9d24094ceccbbe0ec067b2edde1f53a9ff))

- **agents**: State the interface-write guardrail as a verifiable invariant
  ([`539835c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/539835c0b60a1bb40b39a25444dd6bd29a409461))

- **signals**: Align three renderer docstrings with the render/send split
  ([`031b145`](https://github.com/marcinpsk/netbox-nso-plugin/commit/031b145a83bc63a3cade14ab3ba40836dd61cea6))

### Refactoring

- **reconcile**: Drop the unused last-apply job readers
  ([`35f2f3b`](https://github.com/marcinpsk/netbox-nso-plugin/commit/35f2f3b3f863870ea75bb4cbadac7a1c78fb3187))

### Testing

- Harden plugin review regressions
  ([`35c76d9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/35c76d90629b5e9a02428cd304f7669cfa7bc8b1))

- Read repository files as UTF-8 in the source scans
  ([`913c2af`](https://github.com/marcinpsk/netbox-nso-plugin/commit/913c2afeb2076f3a0eb20e47091aaef02de3f4a4))

- Resolve the full-review test-quality findings
  ([`d582489`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d5824899e528e1613165e0ca2ec856b63abd081f))

- Three assertion-quality fixes from the full review
  ([`d00b225`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d00b2255e571c48e24719c00fcc9e529c45d1239))

- **adapter-client**: Pin the routes member, not the whole wire body
  ([`6be56b8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6be56b8942ee88c800751bfab0641f76968fda10))

- **apply**: Capture preparation deadlines in call order
  ([`e2814dd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e2814ddf7ee0056286371ab5ad27a166381147d5))

- **apply**: Isolate the SNMP drain boundary
  ([`765c7b5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/765c7b5bedb04bc922439303acada56423402c84))

- **apply**: Patch the drain clock seam in the deadline tests
  ([`b2ec16a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b2ec16ad0584da13721daa9a148435e93cbeb219))

- **apply**: Pin the exhausted budget against both send seams
  ([`82e9a41`](https://github.com/marcinpsk/netbox-nso-plugin/commit/82e9a411e0483967d7c3961039af55310cc7db75))

- **claim**: Pin the success hook through a coalesced route-policy claim
  ([`af547bf`](https://github.com/marcinpsk/netbox-nso-plugin/commit/af547bfb2453cf6753017e968ed9f8193dc4ec9a))

- **commands**: Pin the --scope refusal on a key the typo cannot spell
  ([`6f608be`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6f608bedff52e6ed9441c2f39ae28760c1a4e724))

- **deadline**: Release the paused connect on every abort outcome
  ([`557e89f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/557e89f3649d03000279e9c8b60e65da0d14e82d))

- **delivery**: Derive scope isolation from the registry instead of by hand
  ([`7d8c469`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7d8c4691e1531d5c0cf8a7706f069f3a496acc90))

- **delivery**: Make the out-of-protocol header pin discriminating
  ([`5b2abd2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5b2abd20527ececd3250b683937ebc4b68b84ff6))

- **drift**: Read the drain verdict from the constant that owns it
  ([`bb87c32`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bb87c329715560fbb829ca26ea8839682cfa04db))

- **guards**: Scan the two sibling AST guards on the plugin-relative path
  ([`695478d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/695478d19238b4aaae0c04d0af29a4a9f18ce3cb))

- **hardening**: Close three pins that could pass while the property was broken
  ([`bbed96d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bbed96de9ec6904f621f3052511645f8815ddcf9))

- **link-role**: Assert the OSPF negative over every push call
  ([`4603285`](https://github.com/marcinpsk/netbox-nso-plugin/commit/46032858dc9af971ba08b28a8bdc988d3979d1c5))

- **link-role**: Chain setUp so the intent-push reset actually runs
  ([`80d36fe`](https://github.com/marcinpsk/netbox-nso-plugin/commit/80d36fe89ff12316cdc430aa610ffbcbfbf4e722))

- **lint**: Bind the zizmor pin to executed workflow references
  ([`45462cc`](https://github.com/marcinpsk/netbox-nso-plugin/commit/45462cc18144837511ef0c1b7086561628a8005f))

- **lint**: Pin zizmor across its four sources
  ([`5e0bf17`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5e0bf172e55e62ed0e743ba88fb32d8fc3801820))

- **lint**: State the literal-pin contract of the zizmor guard
  ([`47f3454`](https://github.com/marcinpsk/netbox-nso-plugin/commit/47f345400fe1f09bd97f74c319d2f7523493b911))

- **migrations**: Restore the rollback test to the graph's leaves
  ([`623f637`](https://github.com/marcinpsk/netbox-nso-plugin/commit/623f63761aeb423a3cbc55e5d210e666455379d2))

- **mixins**: Answer isolated push_now with a settled count
  ([`2563afa`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2563afaa449923697cfd7f8edf79874b76aba03d))

- **outbox**: Clean up acknowledgement appender
  ([`c2b1792`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c2b1792799050ea7822f5b7713a38cfed0a6f8cf))

- **outbox**: Make the review's eight test-quality findings real pins
  ([`cbf675d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cbf675da1ec2a92aa7e32c24a76a24df91e0f5ae))

- **outbox**: Release and join the marking writer via cleanups
  ([`fe37c6e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/fe37c6ee409eb2537b08e380b55d31dfa04bbaa7))

- **outbox**: The round-trip pin follows the sequence's new reverse
  ([`b94f8fa`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b94f8fa10c3b6fb1b80ea07f2443429e003ddc5d))

- **pins**: State two properties the review had to infer from the code
  ([`8c083b9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8c083b9ae93f0120e9de62ef24d92c5a393a8036))

- **release**: Read repository files with explicit utf-8
  ([`4621aa8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4621aa8a3f2fb3c1fb73bda9e3e343a2d5939796))

- **release**: Reject duplicate locked package entries
  ([`4c5c7e5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4c5c7e56aa644a03ee1db5f28228e73801829927))

- **review**: Harden release and SNMP pins
  ([`1c1e7c8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1c1e7c851498d8a6f5b0272520b16c10e57d7dda))

- **settlement**: Suppress the intent push around the logging fixture
  ([`eef8423`](https://github.com/marcinpsk/netbox-nso-plugin/commit/eef84236c73289205cd84ee3922d8cfa9495a27d))

- **signals**: Pin the AdapterError contract for direct deliver callers
  ([`5f4b3e5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5f4b3e5539bf2c13c50992cf33a0fadb29791235))

- **snmp**: Derive the delivery adapter identity
  ([`ecb584c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ecb584c76e6f5fe6ab9fe584f234d13ad401b079))

- **snmp**: Derive the harvest adapter identity
  ([`f29bdc3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f29bdc3a0f2f3c65bde708684cc6fe8b5b3bb2ed))

- **vlan**: Reset scheduled intent state in TestVlanReconciler
  ([`9d5f91e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9d5f91e00b70559d97813a7e84692222f06adfcd))


## v0.3.0 (2026-08-18)

### Bug Fixes

- **adapter-client**: Log the transport exception type, never its text
  ([`23b396f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/23b396f2813cb521d1b35c5b3e2ca81a183a0585))

- **devcontainer**: Pin netbox-test-django at a non-resolving adapter
  ([`97a298a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/97a298a234ecdf76a921b112a1ed07a263295b44))

- **logging**: Survive a concurrent delete of the levels singleton
  ([`1cb8290`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1cb829093c0ef787b09a6e102e516e91f6c04ad5))

- **reconcile**: Re-read the apply state after the settlement consumes a repair
  ([`c155b09`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c155b0942ee5955a62740a30980472cdb196ae65))

- **review**: Harden test and timestamp boundaries
  ([`0b76e4a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0b76e4afe3a67cc586de98cd45573972dd7258bb))

- **review**: Preserve valid sync timestamps
  ([`cad4a34`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cad4a34ce9287294aff812940ba2658218013654))

- **security**: Escape the unknown-category key in the 400 body
  ([`b7cab70`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b7cab70ce0263191b68b750623d98ef5d1103bdd))

- **security**: Report adapter failures by exception type, not text
  ([`226f0c2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/226f0c217aae40aeb81a909282dacfa3433d8eab))

- **settlement**: Bind the reused apply probe to the device the consumer locked
  ([`aa53804`](https://github.com/marcinpsk/netbox-nso-plugin/commit/aa53804ae6567adfe80a2f39ac7b91dcf016a219))

- **settlement**: Bound a feed row with no sequence, and read back once per pass
  ([`8374d82`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8374d827f7968a93cce080ab84815c0c0256e1b0))

- **settlement**: Skip a feed row with no sequence instead of stalling on it
  ([`7431567`](https://github.com/marcinpsk/netbox-nso-plugin/commit/743156749b5938e7fc68d1aaea2b3501e0eb1e0b))

- **sync-cache**: Degrade non-string and offset-less adapter timestamps
  ([`c9b7007`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c9b7007ec6cac883c2590a3452af0cb5addef845))

- **test**: Require a private database name
  ([`658d3a6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/658d3a605bb4c63f24429011e3293a089174a4c2))

- **test**: Spawn the restart consumer under the standard settings
  ([`340c878`](https://github.com/marcinpsk/netbox-nso-plugin/commit/340c878e4f3ed3e221ef3e2d2ed150943acce428))

### Chores

- **ci**: Bump astral-sh/setup-uv in the actions group
  ([`f7a3ed2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f7a3ed2eaaaedb75b8b163ad6f40ea1173141f9b))

- **deps**: Bump jsdom from 26.1.0 to 30.0.1
  ([`3b712fd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3b712fdb8e51bbfd859f40bfccf285c9b2f77f00))

- **deps**: Bump vitest from 3.2.7 to 4.1.10
  ([`74a5d8a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/74a5d8aba0a8c34336e1c599633c43b150a4dc61))

- **deps**: Update django requirement from <7.0,>=5.1 to >=6.1,<7.0
  ([`28e0866`](https://github.com/marcinpsk/netbox-nso-plugin/commit/28e0866c6a9299449fdaf5b13184fb1de447d2a9))

- **deps**: Update mkdocs requirement from <2,>=1 to >=1.6.1,<2
  ([`f3b26f9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f3b26f9f29d0bac30d623765c0c69249b83424d4))

- **deps**: Update pytest-cov requirement from >=6.0 to >=7.1.0
  ([`2addffb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2addffb569b4863d40b4c387733be64a585d3c41))

- **deps**: Update requests requirement from >=2.32 to >=2.34.2
  ([`0a619ae`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0a619ae2f32b2165f0f59f34e50a824567228d59))

- **test**: Run pytest on capped xdist workers
  ([`5ed9927`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5ed9927956f50882bb9342b7d5c2711057f1bbc8))

### Code Style

- Drop em-dashes from the text this branch added
  ([`9f4d5d4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9f4d5d42e2a52bde62c2efdd2ad7fe6976a997c6))

### Continuous Integration

- Bound the quick workflow jobs with a job timeout
  ([`69b7fc9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/69b7fc9b236848945263f153ac52482c48e138f4))

- Give lint-format and js-test a read-only GITHUB_TOKEN
  ([`c87c629`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c87c6296d29bfb1d0b317cc81c8a0361e068b4df))

- **release**: Guard refs with expected tip lease
  ([`cceca5f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cceca5f92f753e21dbcfe8cff6ec13f2bd68ea3b))

- **release**: Skip a stale release trigger instead of resetting to it
  ([`542de23`](https://github.com/marcinpsk/netbox-nso-plugin/commit/542de23892b919f3d32398e23c84ac089ddd3d7f))

### Documentation

- **settlement**: State the stall bound in terms of feed entries
  ([`65dd1c8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/65dd1c8a702d198fa8b3a539ea26bf59c113134e))

### Features

- **resync**: Report the arming a rejected push rolled back
  ([`65d5070`](https://github.com/marcinpsk/netbox-nso-plugin/commit/65d5070f8a86a3a12a51e6eba09b13861d447a55))

### Performance Improvements

- **reconcile**: Reuse Step 4's apply-job state in the static-route escalation
  ([`02dedcd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/02dedcdc4290a17f964671495bff23ea717f7592))

- **views**: Join nso_instance on the onboarding dashboard's managed rows
  ([`e0eeac0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e0eeac03f6b3535f424d1d73ba407d45fcab5bb0))

### Testing

- **config**: Scope the concurrent-editor injections to the row under test
  ([`8308a31`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8308a31f81249518c446003f2b8aa08e3f2132ec))

- **migrations**: Re-apply every leaf, not just the first
  ([`c2709c0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c2709c0a101c17841d90bbbd64e8b955fab9c660))

- **reconcile**: Exercise the settlement window on a real transaction boundary
  ([`ff49962`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ff499621cdfa69008723ae0df4579263ecab85ab))

- **reconcile**: Patch the forced SNMP, logging and L2 SAP pushes in the apply pin
  ([`081beeb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/081beeba1c702273326696393a6f1d20f8894ff3))

- **release**: Pin bare remote branch
  ([`3570b5d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3570b5dc247e042dc8249fc19b4b69fce0ca45d2))

- **review**: Close isolated settings coverage gaps
  ([`ca7a95e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ca7a95e277a94d9396115382b8ea76948bafff29))

- **settlement**: Anchor the carrier barrier on the consumer entry point
  ([`3c33993`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3c3399367864303946d2d3819cefbbcf73d49091))

- **signals**: Pin that a fail-closed rekey never reaches sync_notify
  ([`2f06b35`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2f06b35bfdbcf0575b76dba619be832a56e8f99f))

- **static-route**: Inject the reclassification on the accept update
  ([`6cb5dce`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6cb5dcec3b2fc180cdfe2f8738fbcb35e318fbb1))

- **static-route**: Share the P2/P6 fixtures and reset the coalescer in a finally
  ([`789e058`](https://github.com/marcinpsk/netbox-nso-plugin/commit/789e0581dc15b9f27b2e8505f4ee80d4109c6bbd))


## v0.2.0 (2026-08-10)

### Bug Fixes

- **apply**: Promote static routes only on an acknowledged stored count
  ([`4e80f37`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4e80f372962840409d0fa6bc03c1228c253c100d))

- **reconcile**: Read nso_management defensively in the settle step
  ([`509fa10`](https://github.com/marcinpsk/netbox-nso-plugin/commit/509fa106bf7aef56c96e66d4c16f5fc48641dbe1))

- **static-route**: Accept only a real route count as a stored acknowledgement
  ([`4990b6d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/4990b6d2baca70dad65dffc9f0c14e29bd260762))

### Chores

- **deps**: Watch the JS test harness with dependabot
  ([`166f7af`](https://github.com/marcinpsk/netbox-nso-plugin/commit/166f7afa7350bffb141a2e33e2189bc22c9a4f69))

### Continuous Integration

- **test**: Pin the matrix diagonally and honor DB_NAME in the CI configuration
  ([`8041a05`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8041a057ed220536beab0cf5faf353ed2a847199))

### Features

- **jobs**: Report the settlement sweep's elapsed time in the tick summary
  ([`29b4714`](https://github.com/marcinpsk/netbox-nso-plugin/commit/29b4714c0252f3b0c515f422f8182450d1d7a89e))

### Performance Improvements

- **settlement**: Drop the redundant feed request and the unbounded reads
  ([`ac7b8c4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ac7b8c4b4b50d52b72de393307fd59f3663b68f3))

### Refactoring

- **signals**: Attribute a static-route rejection with the push filter
  ([`ab6014f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ab6014f419e673d8eb03c3ee648860355b83dffb))

### Testing

- **apply**: Stop each patch on its own cleanup, and pin the stuck-row message
  ([`2e9f58e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2e9f58ea3d9b3daa8e43f4aa99ffb3ed1aacf365))

- **contract**: Restore all three settings entry states, not just the value
  ([`7cda4b5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7cda4b5a2a559788552839688d5c2a5c65bc8b7e))

- **contract**: Restore the process plugin config the live client replaces
  ([`72aa7c8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/72aa7c89be5cbca1b4af71f991b87ee7bdfa7001))

- **migrations**: Report the pending migration instead of a raw SystemExit
  ([`f1ecdf3`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f1ecdf3e7182e569c21b2bb64c43f81ea06ef212))

- **settlement**: 422 a jobs request with no device_id on either order
  ([`7a7dcc4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7a7dcc49d2a3de5fd471903242f0cca54d6a0905))

- **settlement**: Define the generation-clock helper once, in the shared base
  ([`1d72354`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1d7235436bc3f0941fdc2008cb1d3211ed993661))

- **settlement**: Derive the protected settlement columns, and locate manage.py
  ([`fc35978`](https://github.com/marcinpsk/netbox-nso-plugin/commit/fc35978664552f07f2fd96f1634908d839594353))

- **settlement**: Fail a bounded thread join on its own terms
  ([`6006505`](https://github.com/marcinpsk/netbox-nso-plugin/commit/60065050194060735a76c1ffa7733bc5cc26ace5))

- **settlement**: Judge the adapter's callback on the test thread
  ([`c43c9bd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c43c9bd9f175a5441bfef6ad676f028a32aee790))

- **settlement**: Prove the mirror pass reached the failed-settlement row
  ([`45e4cd1`](https://github.com/marcinpsk/netbox-nso-plugin/commit/45e4cd1ad561262fce4f1bda867adea97c2c2648))


## [Unreleased]

### Added — read paths (NSO → NetBox)

- Core models: `NSOInstance`, `NSODeviceManagement` (managed scopes per device),
  plus `NSO*State` status overlays for every synced family, all driven by a
  unified status state machine (`unknown → imported → accepted → deploying →
  in_sync / apply_failed`, drift → `changed`, faults → `error`).
- Device **NSO tab**: counts-first category summaries with lazy row expansion,
  pagination + search per category, rendered from last-synced state for fast
  loads; one merged per-interface table (enabled / description / IPs / MTU /
  switchport) with per-cell Accept.
- Synced families: interface attributes + IPs + MTU (`mtu`/`ip-mtu`/`mpls-mtu`),
  VLAN database, L2 switchport, SVI/IRB, dot1q subinterfaces, L2 services
  (Nokia epipe/vpls SAPs), LACP/LAG bundles, IS-IS (instances, interfaces,
  levels, segment-routing, Flex-Algo), OSPF, BGP (peers, AFs, peer-group
  templates), route-policy objects, redistribution, static routes, BFD, SNMP
  (secret-safe: hash + vault refs only), logging hosts.
- Reconcilers materialise native NetBox/netbox-routing objects (interfaces,
  IPs, VLANs, ISIS/OSPF/BGP graph, route policies, static routes) and 3-way
  merge device vs NetBox edits without clobbering operator-owned rows.

### Added — write path (NetBox → device, via nso-adapter)

- **Accept → Apply** flow: Accept promotes an imported value to NetBox-owned
  intent (pushed to the adapter intent mirror); a single device **Apply**
  commits all pending scopes through the NSO `*-reconciler` services, with a
  two-panel preview (intent summary + native device diff per scope).
- Greenfield writes for operator-created objects: static routes, VLANs
  (shared-VLAN attach + rename propagation), route policies, OSPF/IS-IS
  interface enablement, IS-IS Flex-Algo, SVIs, subinterfaces, Nokia routed
  sub-interfaces (port binding emitted for never-imported interfaces).
- Inline operator edit on the tab (description, enabled, MTU) with
  clobber-safe value overlays.
- Intent **split-brain detection + one-click re-sync**: per-scope comparison
  of the adapter intent mirror vs NetBox-owned overlays (orphaned and
  partial/count-based), surfaced as a device-tab banner; re-sync re-pushes
  the owned snapshot and never touches the device.
- Drift banner + re-sync for orphaned adapter intent; value-aware drift
  display comparing live NetBox values against the device.
- **Intent-push rejections are recorded and shown.** A push the adapter refuses
  is still swallowed — an unreachable adapter must not raise into the operator's
  save — but the reason is now persisted per (device, scope) on
  `NSODeviceManagement` and rendered as a category banner, instead of living only
  in a log line under a green row. Static-route rows additionally show the apply's
  own per-route error or its `unproven` advisory, and an owned `apply_failed`
  static route no longer renders as "pending apply". Recording only: durable
  retry over the record is tracked separately.
- **Generation-correlated settlement for static routes.** A static-route overlay
  no longer settles on a scope-wide apply counter. Each push stamps the route with
  a plugin-global `intent_generation` and records the fingerprint the adapter
  echoes for it, and the overlay settles only when a per-route apply result names
  **both**. A result naming a generation the overlay has already moved past is
  simply not this row's result: it is skipped and the cursor advances. A result
  naming the **current** generation but a fingerprint this device is not waiting
  for is a disagreement about content, so it does not settle and it records why.
  `unproven` is neither a settle nor a failure: it is kept as an advisory on the row.
  - Results arrive over the adapter's **ordered settlement feed**
    (`GET /api/v1/jobs?order=asc&after_settle_seq=…`), walked under a durable
    per-device cursor on `NSODeviceManagement`. The cursor is keyed on
    *(store incarnation, adapter device id)* and both halves are compared on every
    read — the incarnation against the feed response's `X-Store-Incarnation` header,
    never against a cached mirror — so an adapter store rebuild or a device remap
    resets the cursor instead of silently skipping every settlement below it.
  - A result that cannot be decided (a lost PUT response whose expectation the
    adapter's read-back also fails to re-serve) stalls the device rather than being
    burned. The stall is bounded at **five** attempts, counted per stuck sequence
    and persisted on the row, so the count survives a worker restart; on the fifth
    the cursor advances past it with an error-level log.
  - Two independent clocks consume the feed: the device reconcile (the carrier,
    running ahead of the stuck-`deploying` backstop in the same invocation) and the
    five-minute `RefreshDeviceSyncCacheJob` maintenance tick. The tick runs
    plugin → adapter, so consumption survives a dead adapter → NetBox callback
    channel. A consumer failure stands the static-route backstop down for that
    invocation only, leaving the other scopes' settlement untouched.
  - `manage.py nso_consume_static_route_settlements` walks the feed by hand — the
    operator's drain tool, not the production path.
  - Rows owned before generations existed carry the sentinel `0` and can correlate
    with nothing. `manage.py nso_resync_static_route_intent` arms them in the same
    pass that backfills `route_id` into the adapter's store, and demotes a
    pre-existing `deploying` row to `accepted` so no result is owed for a
    generation that was never sent.
  - **Per-object static-route deletion authority is active end to end.** The intent
    outbox retains each removed route and its acknowledged identity until delivery.
    Static-route pushes carry the authority in `deleted_routes`, the adapter records a
    `delete_origin` tombstone, and its removal worker executes the networked retraction.
    A combined identity and membership edit settles only the retained device.

### Added — operations

- Device onboarding NetBox → NSO (create node, fetch host keys, unlock,
  sync-from) with NED picker and quick-manage from the NSO Devices dashboard.
- Adapter job orchestration from the tab: Sync, Detect drift, Test
  connection, Apply — with client-side job polling and status strip.
- REST API at `/api/plugins/nso/device-management/` consumed by the
  adapter's reconcile loop.
- Deployment-window tooling for adapter store restores:
  `nso_intent_deployment_gate` (`--prepare`/`--verify`/`--abort`) quiesces
  plugin-side writes behind a durable gate while a restore runs, with mutating
  HTTP requests answering 503 until the gate lifts, and `nso_intent_restore`
  rebuilds the outbox from the adapter's replayed receipts: it advances the
  push-seq and static-route pk namespaces past everything the store
  acknowledged, clears delivery lineage, and resolves open claims.

### Changed

- Per-interface scope cards consolidated into the single merged Interface
  table; category templates deduplicated into shared partials.

### Fixed (highlights)

- Owned overlays no longer stuck in `pending`/`deploying` (settle to
  `in_sync` after Apply; `apply_failed` wired).
- Reconcile no longer clobbers owned OSPF/IS-IS interface intent; stale
  overlay rows pruned across all FK reconcilers instead of raising false
  drift.
- Reconcile no longer restores a static route's superseded generation state.
  Its mirror refresh saves an explicit field allow-list, and it writes the
  overlay status only as a compare-and-set against the status it observed, so a
  reconcile that began before a concurrent writer's lock can no longer put back
  the generation, the expectation or the status that writer had just replaced.
- Route-policy Accept crash, VLAN rename surfacing as drift, switchport
  3-way merge seeding, per-scope apply-failure surfacing.
