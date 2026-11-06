project('module moves between providers', 'cpp', default_options: ['cpp_std=c++20'])

# The module "shadowed" moves from aprov to bprov between the two modes, while
# both stay module providers and the consumer links both. aprov's directory is
# searched first, so a stale shadowed BMI left there would shadow bprov's.
moved = get_option('mode') == 'moved'

aprov = static_library('aprov', moved ? ['aother.cppm'] : ['aother.cppm', 'shadowed_a.cppm'])
bprov = static_library('bprov', moved ? ['bother.cppm', 'shadowed_b.cppm'] : ['bother.cppm'])

# The expected value rides a generated header, not a define: a define would
# put the consumer in a BMI class of its own.
expected = configure_file(output: 'expected.h',
                          configuration: {'EXPECTED': moved ? 2 : 1})
test('consumer', executable('consumer', 'main.cpp', expected,
                            link_with: [aprov, bprov]))
