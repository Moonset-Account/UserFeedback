rule test_rule {
    strings:
        $a = "test" wide
        $b = "this" nocase
        $c = { AA BB CC DD EE FF }

    condition:
        any of them
}