# Changelog

<!-- version list -->

## v1.1.0 (2026-09-01)

### Bug Fixes

- **adapter**: Reject non-positive generation identities
  ([`2f18c02`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2f18c0249d9bf6e4f289d09d850150988a342791))

- **adapter**: Resolve generation review findings
  ([`0cc5e77`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0cc5e77cd4cff2d44747255c39ffd9730a13ae0b))

- **adapter**: Validate generation identities
  ([`9c2e2bd`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9c2e2bde7187330901720acc97741f6934fdc1f8))

- **adapter-client**: Reject wrong-typed settlement_cohort values
  ([`43d2aac`](https://github.com/marcinpsk/netbox-nso-plugin/commit/43d2aacc9126877f14d706adff771d330841e1fb))

- **apply**: Close the promotion, accept, and VLAN lock-order hazards
  ([`645c9c0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/645c9c0a1998c3848f8623573f622349628589d3))

- **apply**: Release the rows no generation promoted when the head job is bad
  ([`5b13c13`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5b13c13b80fa7fa4a5c9b17e97da5816a21d4c61))

- **apply**: Resolve review findings
  ([`2800646`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2800646717681c2e95b99c948eb6d1196886cacc))

- **apply**: Resolve the PR #20 review wave on the repair subsystem
  ([`7a7f535`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7a7f5352b10d68a270a9250c449ab91881f6acee))

- **apply**: Resolve the PR #20 second and third review waves
  ([`2394ff5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2394ff54c48ea1ab1dd7c43fd601db282c57714f))

- **apply**: Roll back malformed no-op partitions
  ([`282eade`](https://github.com/marcinpsk/netbox-nso-plugin/commit/282eadef4cec025ced56cd1e10ad3fd6bbc4055c))

- **apply**: Roll back malformed no-op results
  ([`9b2a5b5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9b2a5b5ee07960c73e6769d68ae7ce5351ec0e4b))

- **apply**: Type the Apply refusals so the handler can rebuild their wording
  ([`0352888`](https://github.com/marcinpsk/netbox-nso-plugin/commit/035288860b77dabd30d730970377e1897cc8d14a))

- **commands**: Bound the watermark advance and name a stall
  ([`dea1efc`](https://github.com/marcinpsk/netbox-nso-plugin/commit/dea1efc01e8eed226f3157a8bea1b21f119c8e6a))

- **contract**: Advance generation endpoint pin
  ([`6be3a53`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6be3a538b2fb7b6115003bf5ba9aafc8de086f30))

- **deploy**: Preserve active claim recovery
  ([`426430e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/426430eb4bb51bec7d1f028539f40eaecadaf784))

- **deployment**: Bound gate transitions
  ([`779fa7d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/779fa7d82aafc6642fd0bc59f8eb5a6e2f2ad590))

- **deployment**: Keep plugin delivery outside transactions
  ([`e4e3e36`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e4e3e3616158e8d1cc9f5b915216906a345e3437))

- **deployment**: Roll back refused HTTP mutations
  ([`f9d22af`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f9d22af7fa82a9d5ddac98cfdfae215064302573))

- **drain**: Keep the first successful sequence the selector names
  ([`882ca87`](https://github.com/marcinpsk/netbox-nso-plugin/commit/882ca87fdcb5c73ef33b09a095874a536dceda73))

- **drain**: Keep the latency chain out of the caller's capture
  ([`2bf1283`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2bf1283759f8031442710eebd24627858caae80a))

- **drain**: Restore the recorder the Apply selector reads
  ([`f542fc1`](https://github.com/marcinpsk/netbox-nso-plugin/commit/f542fc18f04419a62b7c9c384494a9c1b88170cb))

- **gate**: Return the quiesce refusal as json the tab can read
  ([`09af80b`](https://github.com/marcinpsk/netbox-nso-plugin/commit/09af80b91c42ee56ec781f12c3049eb10c739df0))

- **intent**: Close review race gaps
  ([`12db008`](https://github.com/marcinpsk/netbox-nso-plugin/commit/12db00880322f911a70d9fde2400d83ab9205105))

- **intent**: Enforce deletion and edit invariants
  ([`b3d3a4c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b3d3a4c5e01760dfbd239e39c78db18e0cb8cc46))

- **intent**: Preserve deployment claim semantics
  ([`28fdbc0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/28fdbc0df4420d2b83328e8d54cbd95600a4edfc))

- **intent**: Preserve scoped deployment state
  ([`c91f35a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c91f35aeeefdbaffad0206703347e615a0d5b009))

- **o3c**: Redact the store credential from the adapter log text
  ([`6bc2795`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6bc279548a6824f8928a9d87ae749cef1adff77c))

- **outbox**: Clarify maintenance operations
  ([`88d9f8e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/88d9f8ecab33fb621cccf9321a6d61f49c7cea4a))

- **outbox**: Harden deployment boundary handling
  ([`3a02560`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3a025608b333fb9d351863d7d396a6b247e1c655))

- **reconcile**: Run the step-4 settlers under the intent-push suppression
  ([`78b2fd7`](https://github.com/marcinpsk/netbox-nso-plugin/commit/78b2fd754723dcbce80cce6b24a26b040db9097f))

- **reconcile**: Write both coarse verdicts through a compare-and-set
  ([`89dab38`](https://github.com/marcinpsk/netbox-nso-plugin/commit/89dab38c17cc31de6a9b0753991e757072e88025))

- **restore**: Preserve adapter failure guidance
  ([`51a0da2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/51a0da2875d7dc80feb9f46595b6ea81b663818d))

- **review**: Bound apply polling and repair regressions
  ([`a32d181`](https://github.com/marcinpsk/netbox-nso-plugin/commit/a32d1816bfd9794a6ebf790fed2cbe238010a048))

- **review**: Close apply boundary gaps
  ([`657962b`](https://github.com/marcinpsk/netbox-nso-plugin/commit/657962ba05baa1e8445e9f9b90c078e5fd908e2c))

- **review**: Close deployment gate review gaps
  ([`6cd426c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6cd426cee1e5eac97f0adb54903236be79cfa8c1))

- **review**: Harden apply generation handling
  ([`b9be568`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b9be568b95c817a8f5b31b910b141e90edd41fd7))

- **review**: Harden deployment activation
  ([`0675ecb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0675ecb7255c0aa3ef97f4136e477b25604c4bcd))

- **review**: Harden deployment and outbox test seams
  ([`adfe7a8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/adfe7a86b8d6b1e22d28fd4f77400c46bc3ddb98))

- **review**: Make deployment locking fair
  ([`8ac2170`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8ac2170985e582856e5c8248016ca4df7a3cce7f))

- **review**: Pause scheduled maintenance cleanly
  ([`7f3c6cb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7f3c6cb0422f8c5195ab7695e8950db8a2a6c7b9))

- **review**: Require a valid apply head job
  ([`2fb6458`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2fb6458d7967cac6ec59103c243898b81fc83734))

- **review**: Resolve the apply-selector review findings
  ([`39a741a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/39a741a7a6f6ce1dbec810d1da61201753de499d))

- **review**: Resolve the o3 review findings and guard outage compaction
  ([`07e7426`](https://github.com/marcinpsk/netbox-nso-plugin/commit/07e742626c6355f94f5f58b778cf2db40f202be4))

- **review**: Scope deployment waiters to the database
  ([`2c454c6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2c454c6bd89324b67ca49e57b177c8cab7f389a4))

- **review**: Tolerate successful outbox worker exit
  ([`7d10299`](https://github.com/marcinpsk/netbox-nso-plugin/commit/7d102998215a3b3bd3199813ecdacd3cfcfb042d))

- **review**: Verify apply generation provenance
  ([`61949d1`](https://github.com/marcinpsk/netbox-nso-plugin/commit/61949d1ba8c138248093f2a22ce863e1481449f4))

- **tab**: Refresh the categories on any terminal chain outcome
  ([`249294e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/249294e9fe6a95bd486f4498e509a8573280279a))

- **test**: Floor the push sequence before fabricating a lower receipt
  ([`17e4f7a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/17e4f7ad012330f2228e691602e67faa73a507b5))

- **test**: Keep delivery seam state read-only
  ([`d0c8fb7`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d0c8fb7fa46c8765c71189d815af2eb0ec2c6d32))

- **test**: Narrow adapter port allocation race
  ([`8c9dc70`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8c9dc700ad39ded38275a0f2f064edc5fea3fb94))

- **test**: Restore apply polling timeout spy
  ([`cda2dad`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cda2dad235b940c179f95cb7e8c9fe49b4d3c149))

- **vlan**: Harden native intent locking
  ([`12a54c0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/12a54c0328e665e9ff7f2e5b781694e879c91c20))

- **vlan**: Reject late rescope attachments
  ([`8a470bb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8a470bb6bf614f098b627109f4aff7b6f2f9e8e6))

- **vlan**: Report qinq name collisions
  ([`a515d29`](https://github.com/marcinpsk/netbox-nso-plugin/commit/a515d29da84acb67ad81e09fdbd4f7f03a82881f))

### Chores

- **deps**: Bump pymdown-extensions from 10.21.3 to 11.0.1
  ([`bf3c383`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bf3c3832a98e087f404e06995fdfd8dd4361e703))

- **deps**: Bump sqlparse from 0.5.5 to 0.6.0
  ([`bdf5a73`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bdf5a73e29739eb5244320e20725752a009be9ca))

### Code Style

- Drop the em-dashes from the two review-fix comments
  ([`d8e7a06`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d8e7a068bc728a70bf460d63631b957303950bc0))

- **test**: Drop the shadowing local requests import
  ([`ae2e1b0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ae2e1b0c4d42f291d33806908f0cd8d6f9087dd6))

### Documentation

- Name trigger_apply's real signature
  ([`a97395a`](https://github.com/marcinpsk/netbox-nso-plugin/commit/a97395af1600ac3d6ee6ed92f3e611a857b21333))

- **ui**: Truthful conflict wording for the heterogeneous admission rules
  ([`6e1ba50`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6e1ba50e713b4a35181a2f52d1e3f7a91ca4d686))

### Features

- **apply**: Join the manual Apply to the adapter selector contract (#1558 2c)
  ([`ddac90e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ddac90e2ffb0132b158308bf148bc2f5a254900a))

- **outbox**: Activate per-object deletion authority for static routes
  ([`0ce36cb`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0ce36cb38277d9ced6ff14b37d042b29d8d66f5e))

- **outbox**: Add the deployment gate and restore commands
  ([`0ca2162`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0ca2162ee5b93a973cf9d23b3bc705acc1de9a76))

### Refactoring

- **intent**: Validate persisted claim flags
  ([`331a6a5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/331a6a5a2427a04639485cebe311fef1e43c89cd))

- **outbox**: Share state authority folding
  ([`17c2335`](https://github.com/marcinpsk/netbox-nso-plugin/commit/17c2335ba02336e93fcca25b143918738b7c89c5))

### Testing

- Drop the routing patches isolate_other_scopes already installs
  ([`8dc48e1`](https://github.com/marcinpsk/netbox-nso-plugin/commit/8dc48e126bd2f3de11da997c5619a88ea30189fd))

- Exercise registered review paths
  ([`56d9c01`](https://github.com/marcinpsk/netbox-nso-plugin/commit/56d9c01e2f77fcb24d73bcd122a8dce912c27c73))

- **adapter-client**: Pin the deleted_routes default on the wire
  ([`67b04f9`](https://github.com/marcinpsk/netbox-nso-plugin/commit/67b04f9b0f64a201e92cb6854a28316c3ec1a2ad))

- **apply**: Derive page limits and drain verdicts from their constants
  ([`27250ed`](https://github.com/marcinpsk/netbox-nso-plugin/commit/27250edcbf13473186f340f31e5d8e694ece4d58))

- **apply**: Follow the device-action conflict to 409 on this branch
  ([`715f233`](https://github.com/marcinpsk/netbox-nso-plugin/commit/715f2333a74fcb2f1970cd152fde265b6600f46b))

- **apply**: Pin fail-fast SNMP ordering
  ([`24c988c`](https://github.com/marcinpsk/netbox-nso-plugin/commit/24c988ce31e6b8e67a978c6e01586d0797dfefb7))

- **apply-selector**: Patch the drain send clock the Apply now reads
  ([`d7f78df`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d7f78df54bc64c2e72423229e094d99fe81beebb))

- **contract**: Pin generation listing support
  ([`b5b5ca8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b5b5ca8f869de55096954f228fba0342e4e87e10))

- **contract**: Send required push sequence
  ([`6ac0a72`](https://github.com/marcinpsk/netbox-nso-plugin/commit/6ac0a72b08f4086b7fea6fabbf7298fabdfcda2f))

- **delivery**: Scan the receipt-literal guard on the plugin-relative path
  ([`e9b70d6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/e9b70d6d1ab0d264aadd4f2ef43efe1d18192953))

- **deployment**: Harden lock worker cleanup
  ([`96ac699`](https://github.com/marcinpsk/netbox-nso-plugin/commit/96ac699b666bbe9a61f13584dc4892da2ea73c95))

- **drain**: Assert the tick's durations from the rendered log record
  ([`1f7062f`](https://github.com/marcinpsk/netbox-nso-plugin/commit/1f7062fea615e4d745c4799bffa0a6cee52cf0e8))

- **drain**: Let the gate pin's own diagnostic reach the runner
  ([`54a57b0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/54a57b08cb462e3968634ef3b379c10e5a79d5d0))

- **intent**: Strengthen review regression pins
  ([`802e2ef`](https://github.com/marcinpsk/netbox-nso-plugin/commit/802e2ef77d475aafd60054244a016d01edd2f65b))

- **intent**: Use exact model save paths
  ([`3077a72`](https://github.com/marcinpsk/netbox-nso-plugin/commit/3077a729f443e3518f77ba773bcceef1a15b885b))

- **lacp**: Execute deferred no-device check
  ([`5b17ad6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5b17ad6253ac513247b97ef6e3ba775da43be7e5))

- **o3c**: Name a missing pinned-checkout file
  ([`b335129`](https://github.com/marcinpsk/netbox-nso-plugin/commit/b335129466b4c0ac024f838972030ac45969d779))

- **o3c**: Redact before truncating, and release every resource
  ([`675d6a6`](https://github.com/marcinpsk/netbox-nso-plugin/commit/675d6a67957c34a4860fe9e05a0dbce18a12aa95))

- **o3c**: Release every resource when the adapter will not die
  ([`eea75a7`](https://github.com/marcinpsk/netbox-nso-plugin/commit/eea75a7798d52173a3c53902b4aa6a4b421a71bf))

- **o3c**: Release failed class setup
  ([`c6a139b`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c6a139bd239974543a7181f6d52ff96ea7953d89))

- **o3c**: Skip the joined class when the adapter worktree is missing
  ([`d392a4e`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d392a4e91b40a11438dae860f51568368b30f72d))

- **outbox**: Assert the maintenance gate refusal
  ([`5c8a3bc`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5c8a3bc1d97c2e08323ca40e64dedf1c47be34fd))

- **outbox**: Derive migration parent from the graph
  ([`d2129c5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d2129c514889bea5113f5cd678e06db46a01858e))

- **outbox**: Exercise state cleanup on delivery
  ([`a4feeb2`](https://github.com/marcinpsk/netbox-nso-plugin/commit/a4feeb2f42f98dc4fe5a0ad5dbf915b0e92d4e96))

- **outbox**: Expect preparatory backfill outcome
  ([`760a699`](https://github.com/marcinpsk/netbox-nso-plugin/commit/760a6993fadf5986de34304fa4cd6e9e1e088382))

- **outbox**: Inject transport failures as requests errors
  ([`ba65404`](https://github.com/marcinpsk/netbox-nso-plugin/commit/ba65404f755be0eeaf111c33e4736dd4b42ef9ab))

- **outbox**: Pin the joined cross-repository deletion authority (O3c)
  ([`0277c10`](https://github.com/marcinpsk/netbox-nso-plugin/commit/0277c10e9bf13997073af511c2ea2960428f8e89))

- **outbox**: Pin the stale-key path on the send seam
  ([`bd32465`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bd32465b321dbac32c2e28b8344d8cb9be83b2a0))

- **outbox**: Point the delivery-double tests at the send seam
  ([`c547908`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c54790898d4d164e5e2e84910ecd3eccdce9ca57))

- **outbox**: Preserve authority in signal harness
  ([`9e6c6d5`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9e6c6d5d5b829e68933d216ab18a4eeca4907e51))

- **outbox**: Prove the send seam is reached in the failure cases
  ([`df0c600`](https://github.com/marcinpsk/netbox-nso-plugin/commit/df0c600fae8698569f90c7e975c9e8ec62e88446))

- **reconcile**: Name the drain outcome by its constant
  ([`bb93ed0`](https://github.com/marcinpsk/netbox-nso-plugin/commit/bb93ed0d69743def114d7a8aa6e19a1383cb1ea8))

- **reconcile**: Release only the deployment gate this test activated
  ([`dc7f65d`](https://github.com/marcinpsk/netbox-nso-plugin/commit/dc7f65d25757a9765922427c3c05766dbf782ff4))

- **review**: Isolate local process regression traffic
  ([`2cbfa15`](https://github.com/marcinpsk/netbox-nso-plugin/commit/2cbfa15ba58881348c8d8ffe03afe222f216f065))

- **review**: Strengthen deployment pin checks
  ([`9039ea4`](https://github.com/marcinpsk/netbox-nso-plugin/commit/9039ea4db06516aa01682ca9ea688b591a597fec))

- **settlement**: Give the doubles the adapter's own job-id and device-id types
  ([`c073482`](https://github.com/marcinpsk/netbox-nso-plugin/commit/c07348290645cd60ec7883ad964c63efdf93fee8))

- **settlement**: Model the paginated generations contract in the fake
  ([`5635ee8`](https://github.com/marcinpsk/netbox-nso-plugin/commit/5635ee85059b4f9405ff0be0d0797721af42a767))

- **tab**: Give the Apply-chain fixtures the real response shape
  ([`d0d9e80`](https://github.com/marcinpsk/netbox-nso-plugin/commit/d0d9e80f879fc831b7c637565f9ef66962ad071b))

- **ui**: Accept uppercase script tags
  ([`cf2ef25`](https://github.com/marcinpsk/netbox-nso-plugin/commit/cf2ef252821f79b0a51b9880fbfb28aea4269fa6))


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
