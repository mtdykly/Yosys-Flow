module demo(input s, t, a, b, c, d, clk, input [3:0] bus,
            output reg y, overwritten, q, output reg [3:0] partial);
    always @* begin
        if (s) begin
            if (t)
                y = a;
            else
                y = b;
        end else
            y = c;
    end
    always @* begin
        overwritten = a & b;
        overwritten = c ^ d;
    end
    always @* begin
        partial = bus;
        partial[1:0] = 2'b01;
    end
    initial q = 1'b1;
    always @(posedge clk) begin
        q <= a;
        q <= b;
    end
endmodule
